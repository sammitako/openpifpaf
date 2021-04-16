import argparse
from collections import defaultdict
import heapq
import logging
import time
from typing import List

import numpy as np
import torch

from .decoder import Decoder
from ..annotation import Annotation
from . import utils
from .. import headmeta, visualizer

# pylint: disable=import-error
from ..functional import caf_center_s, grow_connection_blend

LOG = logging.getLogger(__name__)


class DenseAdapter:
    def __init__(self, cif_meta, caf_meta, dense_caf_meta):
        self.cif_meta = cif_meta
        self.caf_meta = caf_meta
        self.dense_caf_meta = dense_caf_meta

        # overwrite confidence scale
        self.dense_caf_meta.confidence_scales = [
            CifCafTorch.dense_coupling for _ in self.dense_caf_meta.skeleton
        ]

        concatenated_caf_meta = headmeta.Caf.concatenate(
            [caf_meta, dense_caf_meta])
        self.cifcaf = CifCafTorch([cif_meta], [concatenated_caf_meta])

    @classmethod
    def factory(cls, head_metas):
        if len(head_metas) < 3:
            return []
        return [
            DenseAdapter(cif_meta, caf_meta, dense_meta)
            for cif_meta, caf_meta, dense_meta in zip(head_metas, head_metas[1:], head_metas[2:])
            if (isinstance(cif_meta, headmeta.Cif)
                and isinstance(caf_meta, headmeta.Caf)
                and isinstance(dense_meta, headmeta.Caf))
        ]

    def __call__(self, fields, initial_annotations=None):
        cifcaf_fields = [
            fields[self.cif_meta.head_index],
            np.concatenate([
                fields[self.caf_meta.head_index],
                fields[self.dense_caf_meta.head_index],
            ], axis=0)
        ]
        return self.cifcaf(cifcaf_fields)


class CifCafTorch(Decoder):
    """Generate CifCaf poses from fields.

    :param: nms: set to None to switch off non-maximum suppression.
    """
    connection_method = 'blend'
    occupancy_visualizer = visualizer.Occupancy()
    force_complete = False
    force_complete_caf_th = 0.001
    greedy = False
    keypoint_threshold = 0.15
    keypoint_threshold_rel = 0.5
    nms = utils.nms.Keypoints()
    nms_before_force_complete = False
    dense_coupling = 0.0

    reverse_match = True

    def __init__(self,
                 cif_metas: List[headmeta.Cif],
                 caf_metas: List[headmeta.Caf],
                 *,
                 cif_visualizers=None,
                 caf_visualizers=None):
        super().__init__()
        self.cif_metas = cif_metas
        self.caf_metas = caf_metas
        self.skeleton_m1 = np.asarray(self.caf_metas[0].skeleton) - 1
        self.keypoints = cif_metas[0].keypoints
        self.score_weights = cif_metas[0].score_weights
        self.out_skeleton = caf_metas[0].skeleton
        self.confidence_scales = caf_metas[0].decoder_confidence_scales

        self.cif_visualizers = cif_visualizers
        if self.cif_visualizers is None:
            self.cif_visualizers = [visualizer.Cif(meta) for meta in cif_metas]
        self.caf_visualizers = caf_visualizers
        if self.caf_visualizers is None:
            self.caf_visualizers = [visualizer.Caf(meta) for meta in caf_metas]

        self.cif_hr = None

        # init by_target and by_source
        self.by_target = defaultdict(dict)
        for caf_i, (j1, j2) in enumerate(self.skeleton_m1):
            self.by_target[j2][j1] = (caf_i, True)
            self.by_target[j1][j2] = (caf_i, False)
        self.by_source = defaultdict(dict)
        for caf_i, (j1, j2) in enumerate(self.skeleton_m1):
            self.by_source[j1][j2] = (caf_i, True)
            self.by_source[j2][j1] = (caf_i, False)

    @classmethod
    def configure(cls, args: argparse.Namespace):
        """Take the parsed argument parser output and configure class variables."""
        # force complete
        keypoint_threshold_nms = args.keypoint_threshold
        if args.force_complete_pose:
            if not args.ablation_independent_kp:
                args.keypoint_threshold = 0.0
            args.keypoint_threshold_rel = 0.0
            keypoint_threshold_nms = 0.0
        # check consistency
        if args.seed_threshold < args.keypoint_threshold:
            LOG.warning(
                'consistency: decreasing keypoint threshold to seed threshold of %f',
                args.seed_threshold,
            )
            args.keypoint_threshold = args.seed_threshold

        cls.force_complete = args.force_complete_pose
        cls.force_complete_caf_th = args.force_complete_caf_th
        cls.nms_before_force_complete = args.nms_before_force_complete
        cls.keypoint_threshold = args.keypoint_threshold
        utils.nms.Keypoints.keypoint_threshold = keypoint_threshold_nms
        cls.keypoint_threshold_rel = args.keypoint_threshold_rel

        cls.greedy = args.greedy
        cls.connection_method = args.connection_method
        cls.dense_coupling = args.dense_connections

        cls.reverse_match = args.reverse_match
        utils.CifSeeds.ablation_nms = args.ablation_cifseeds_nms
        utils.CifSeeds.ablation_no_rescore = args.ablation_cifseeds_no_rescore
        utils.CafScored.ablation_no_rescore = args.ablation_caf_no_rescore
        if args.ablation_cifseeds_no_rescore and args.ablation_caf_no_rescore:
            utils.CifHr.ablation_skip = True

    @classmethod
    def factory(cls, head_metas):
        if cls.dense_coupling:
            return DenseAdapter.factory(head_metas)
        return [
            CifCafTorch([meta], [meta_next])
            for meta, meta_next in zip(head_metas[:-1], head_metas[1:])
            if (isinstance(meta, headmeta.Cif)
                and isinstance(meta_next, headmeta.Caf))
        ]

    def __call__(self, fields, initial_annotations=None):
        start = time.perf_counter()
        if not initial_annotations:
            initial_annotations = []
        LOG.debug('initial annotations = %d', len(initial_annotations))

        for vis, meta in zip(self.cif_visualizers, self.cif_metas):
            vis.predicted(fields[meta.head_index])
        for vis, meta in zip(self.caf_visualizers, self.caf_metas):
            vis.predicted(fields[meta.head_index])

        cif_hr_init_s = 0.0
        if self.cif_hr is None:
            start_cifhr_init = time.perf_counter()
            self.cif_hr = torch.classes.my_classes.CifHr(
                fields[self.cif_metas[0].head_index].shape,
                self.cif_metas[0].stride)
            cif_hr_init_s = time.perf_counter() - start_cifhr_init

        start_cifhr_reset = time.perf_counter()
        self.cif_hr.reset()
        cifhr_reset_s = time.perf_counter() - start_cifhr_reset
        start_cifhr_fill = time.perf_counter()
        for cif_meta in self.cif_metas:
            self.cif_hr.accumulate(fields[cif_meta.head_index], cif_meta.stride, 0.0, 1.0)
        cifhr_accumulated = self.cif_hr.get_accumulated()
        LOG.debug('cifhr (fill = %.1fms, init = %.1fms, reset = %.1fms)',
                  (time.perf_counter() - start_cifhr_fill) * 1000.0,
                  cif_hr_init_s * 1000.0,
                  cifhr_reset_s * 1000.0)
        utils.CifHr.debug_visualizer.predicted(cifhr_accumulated)

        start_seeds = time.perf_counter()
        seeds = torch.classes.my_classes.CifSeeds(cifhr_accumulated)
        for cif_meta in self.cif_metas:
            seeds.fill(fields[cif_meta.head_index], cif_meta.stride)
        seeds_f, seeds_vxys = seeds.get()
        LOG.debug('seeds = %d (%.1fms)', len(seeds_f), (time.perf_counter() - start_seeds) * 1000.0)

        start_cafscored = time.perf_counter()
        caf_scored = torch.classes.my_classes.CafScored(cifhr_accumulated, -1.0, 0.1)
        for caf_meta in self.caf_metas:
            caf_scored.fill(fields[caf_meta.head_index], caf_meta.stride, caf_meta.skeleton)
        caf_fb = caf_scored.get()
        LOG.debug(
            'cafscored forward = %d, backward = %d (%.1fms)',
            sum(len(f) for f in caf_fb[0]),
            sum(len(f) for f in caf_fb[1]),
            (time.perf_counter() - start_cafscored) * 1000.0)

        occupied = torch.classes.my_classes.Occupancy(cifhr_accumulated.shape, 2.0, 4.0)
        annotations = []

        def mark_occupied(ann):
            joint_is = np.flatnonzero(ann.data[:, 2])
            for joint_i in joint_is:
                width = ann.joint_scales[joint_i]
                occupied.set(
                    joint_i,
                    ann.data[joint_i, 0],
                    ann.data[joint_i, 1],
                    width,  # width = 2 * sigma
                )

        # for ann in initial_annotations:
        #     self._grow(ann, caf_scored)
        #     annotations.append(ann)
        #     mark_occupied(ann)

        for f, (v, x, y, s) in zip(seeds_f, seeds_vxys):
            if occupied.get(f, x, y):
                continue

            ann = Annotation(self.keypoints,
                             self.out_skeleton,
                             score_weights=self.score_weights
                             ).add(f, (x, y, v))
            ann.joint_scales[f] = s
            self._grow(ann, caf_fb)
            annotations.append(ann)
            mark_occupied(ann)

        # self.occupancy_visualizer.predicted(occupied)

        # LOG.debug('annotations %d, %.3fs', len(annotations), time.perf_counter() - start)

        # if self.force_complete:
        #     if self.nms_before_force_complete and self.nms is not None:
        #         assert self.nms.instance_threshold > 0.0
        #         annotations = self.nms.annotations(annotations)
        #     annotations = self.complete_annotations(cifhr, fields, annotations)

        if self.nms is not None:
            annotations = self.nms.annotations(annotations)

        LOG.info('%d annotations (%.1fms): %s', len(annotations),
                 (time.perf_counter() - start) * 1000.0,
                 [np.sum(ann.data[:, 2] > 0.1) for ann in annotations])
        return annotations

    def connection_value(self, ann, caf_fb, start_i, end_i, *, reverse_match=True):
        caf_i, forward = self.by_source[start_i][end_i]
        caf_f, caf_b = (caf_fb[0], caf_fb[1]) if forward else (caf_fb[1], caf_fb[0])
        caf_f, caf_b = caf_f[caf_i], caf_b[caf_i]
        xyv = ann.data[start_i]
        xy_scale_s = max(0.0, ann.joint_scales[start_i])

        only_max = self.connection_method == 'max'

        new_xysv = torch.ops.my_classes.grow_connection_blend(
            caf_f, xyv[0], xyv[1], xy_scale_s, only_max)
        if new_xysv[3] == 0.0:
            return 0.0, 0.0, 0.0, 0.0
        keypoint_score = np.sqrt(new_xysv[3] * xyv[2])  # geometric mean
        if keypoint_score < self.keypoint_threshold:
            return 0.0, 0.0, 0.0, 0.0
        if keypoint_score < xyv[2] * self.keypoint_threshold_rel:
            return 0.0, 0.0, 0.0, 0.0
        xy_scale_t = max(0.0, new_xysv[2])

        # reverse match
        if self.reverse_match and reverse_match:
            reverse_xyv = torch.ops.my_classes.grow_connection_blend(
                caf_b, new_xysv[0], new_xysv[1], xy_scale_t, only_max)
            if reverse_xyv[2] == 0.0:
                return 0.0, 0.0, 0.0, 0.0
            if abs(xyv[0] - reverse_xyv[0]) + abs(xyv[1] - reverse_xyv[1]) > xy_scale_s:
                return 0.0, 0.0, 0.0, 0.0

        return (new_xysv[0], new_xysv[1], new_xysv[2], keypoint_score)

    @staticmethod
    def p2p_value(source_xyv, caf_scored, source_s, target_xysv, caf_i, forward):
        # TODO move to Cython (see grow_connection_blend)
        caf_f, _ = caf_scored.directed(caf_i, forward)
        xy_scale_s = max(0.0, source_s)

        # source value
        caf_field = caf_center_s(caf_f, source_xyv[0], source_xyv[1],
                                 sigma=2.0 * xy_scale_s)
        if caf_field.shape[1] == 0:
            return 0.0

        # distances
        d_source = np.linalg.norm(
            ((source_xyv[0],), (source_xyv[1],)) - caf_field[1:3], axis=0)
        d_target = np.linalg.norm(
            ((target_xysv[0],), (target_xysv[1],)) - caf_field[5:7], axis=0)

        # combined value and source distance
        xy_scale_t = max(0.0, target_xysv[2])
        sigma_s = 0.5 * xy_scale_s
        sigma_t = 0.5 * xy_scale_t
        scores = (
            np.exp(-0.5 * d_source**2 / sigma_s**2)
            * np.exp(-0.5 * d_target**2 / sigma_t**2)
            * caf_field[0]
        )
        return np.sqrt(source_xyv[2] * max(scores))

    def _grow(self, ann, caf_fb, *, reverse_match=True):
        frontier = []
        in_frontier = set()

        def add_to_frontier(start_i):
            for end_i, (caf_i, _) in self.by_source[start_i].items():
                if ann.data[end_i, 2] > 0.0:
                    continue
                if (start_i, end_i) in in_frontier:
                    continue

                max_possible_score = np.sqrt(ann.data[start_i, 2])
                if self.confidence_scales is not None:
                    max_possible_score *= self.confidence_scales[caf_i]
                heapq.heappush(frontier, (-max_possible_score, None, start_i, end_i))
                in_frontier.add((start_i, end_i))
                ann.frontier_order.append((start_i, end_i))

        def frontier_get():
            while frontier:
                entry = heapq.heappop(frontier)
                if entry[1] is not None:
                    return entry

                _, __, start_i, end_i = entry
                if ann.data[end_i, 2] > 0.0:
                    continue

                new_xysv = self.connection_value(
                    ann, caf_fb, start_i, end_i, reverse_match=reverse_match)
                if new_xysv[3] == 0.0:
                    continue
                score = new_xysv[3]
                if self.greedy:
                    return (-score, new_xysv, start_i, end_i)
                if self.confidence_scales is not None:
                    caf_i, _ = self.by_source[start_i][end_i]
                    score = score * self.confidence_scales[caf_i]
                heapq.heappush(frontier, (-score, new_xysv, start_i, end_i))

        # seeding the frontier
        for joint_i in np.flatnonzero(ann.data[:, 2]):
            add_to_frontier(joint_i)

        while True:
            entry = frontier_get()
            if entry is None:
                break

            _, new_xysv, jsi, jti = entry
            if ann.data[jti, 2] > 0.0:
                continue

            ann.data[jti, :2] = new_xysv[:2]
            ann.data[jti, 2] = new_xysv[3]
            ann.joint_scales[jti] = new_xysv[2]
            ann.decoding_order.append(
                (jsi, jti, np.copy(ann.data[jsi]), np.copy(ann.data[jti])))
            add_to_frontier(jti)

    def _flood_fill(self, ann):
        frontier = []

        def add_to_frontier(start_i):
            for end_i, (caf_i, _) in self.by_source[start_i].items():
                if ann.data[end_i, 2] > 0.0:
                    continue
                start_xyv = ann.data[start_i].tolist()
                score = start_xyv[2]
                if self.confidence_scales is not None:
                    score = score * self.confidence_scales[caf_i]
                heapq.heappush(frontier, (-score, end_i, start_xyv, ann.joint_scales[start_i]))

        for start_i in np.flatnonzero(ann.data[:, 2]):
            add_to_frontier(start_i)

        while frontier:
            _, end_i, xyv, s = heapq.heappop(frontier)
            if ann.data[end_i, 2] > 0.0:
                continue
            ann.data[end_i, :2] = xyv[:2]
            ann.data[end_i, 2] = 0.00001
            ann.joint_scales[end_i] = s
            add_to_frontier(end_i)

    def complete_annotations(self, cifhr, fields, annotations):
        start = time.perf_counter()

        if self.force_complete_caf_th >= 0.0:
            caf_scored = (utils
                          .CafScored(cifhr.accumulated, score_th=self.force_complete_caf_th)
                          .fill(fields, self.caf_metas))
            for ann in annotations:
                unfilled_mask = ann.data[:, 2] == 0.0
                self._grow(ann, caf_scored, reverse_match=False)
                now_filled_mask = ann.data[:, 2] > 0.0
                updated = np.logical_and(unfilled_mask, now_filled_mask)
                ann.data[updated, 2] = np.minimum(0.001, ann.data[updated, 2])

        # some joints might still be unfilled
        for ann in annotations:
            self._flood_fill(ann)

        LOG.debug('complete annotations %.3fs', time.perf_counter() - start)
        return annotations