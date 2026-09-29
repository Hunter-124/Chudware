"""The 24 machining features (Features/*.py of the generator), ported to OCP.

Each class keeps the original's bound search, shifter, depth rule and sketch. Randomness comes
from the part's own random.Random (ctx.rng), never the global generator."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
from OCP.BRepFeat import BRepFeat_MakePrism
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.GC import GC_MakeArcOfCircle, GC_MakeSegment
from OCP.Geom import Geom_Circle
from OCP.gp import gp_Ax2, gp_Circ
from OCP.TopoDS import TopoDS_Face

from . import geom, occ, params


@dataclass
class Ctx:
    rng: object                          # random.Random of the part
    stock: tuple = (0.0, 0.0, 0.0)       # stock_dim_x/y/z
    next_instance: int = 0
    log: list = field(default_factory=list)


class MachiningFeature:
    shifter_type = None
    bound_type = None
    depth_type = None
    feat_type = None

    def __init__(self, ctx: Ctx, labels: occ.FaceLabels):
        self.ctx = ctx
        self.rng = ctx.rng
        self.labels = labels
        self.shape = labels.shape
        self.min_len = params.MIN_LEN
        self.clearance = params.CLEARANCE
        self.bounds = []

    # ------------------------------------------------------------------ bounds
    def _get_bounds(self):
        {1: self._bound_1, 2: self._bound_2, 3: self._bound_3, 4: self._bound_inner}[self.bound_type]()

    def _face_filter(self, num_edges):
        result = []
        for face in occ.list_face(self.shape):
            if not occ.is_plane(face):
                continue
            if num_edges == 0:
                result.append(face)
                continue
            normal = occ.normal_to_face_center(face)
            for wire in occ.face_wires(face):
                edges = occ.wire_ordered_edges(wire)
                if len(edges) < 4:
                    continue
                good_edge = [occ.is_line(e) for e in edges]
                for i in range(len(edges)):
                    j = (i + 1) % len(edges)
                    k = (i + 2) % len(edges)
                    if not good_edge[i]:
                        continue
                    if num_edges == 1:
                        result.append([face, edges[i]])
                        continue
                    if not good_edge[j]:
                        continue
                    p = occ.first_vertex(edges[i])
                    pntj = occ.first_vertex(edges[j])
                    pntk = occ.first_vertex(edges[k])
                    if not geom.point_in_polygon(p, np.array([pntj, pntk]), normal):
                        continue
                    if num_edges == 2:
                        result.append([face, edges[i], edges[j]])
                        continue
                    if not good_edge[k]:
                        continue
                    p = occ.last_vertex(edges[k])
                    if not geom.point_in_polygon(p, np.array([pntj, pntk]), normal):
                        continue
                    if num_edges == 3:
                        result.append([face, edges[i], edges[j], edges[k]])
        return result

    def _sample_points_inside_face(self, face):
        tri_pnts, triangles, _, et_map = occ.triangulation_from_face(face)
        tri_pnts = np.asarray(tri_pnts, dtype=np.float64)
        sample_points = []
        for a, b in et_map:
            if len(et_map[(a, b)]) > 1:
                pt1, pt2 = tri_pnts[a], tri_pnts[b]
                if np.linalg.norm(pt2 - pt1) < self.min_len + 2 * self.clearance:
                    continue
                sample_points.append(0.5 * pt1 + 0.5 * pt2)
        for tri in triangles:
            pt1, pt2, pt3 = tri_pnts[tri[0]], tri_pnts[tri[1]], tri_pnts[tri[2]]
            if geom.outer_radius_triangle(pt1, pt2, pt3) < self.min_len + 2 * self.clearance:
                continue
            sample_points.append((pt1 + pt2 + pt3) / 3)
            sample_points.append(0.2 * pt1 + 0.4 * pt2 + 0.4 * pt3)
            sample_points.append(0.4 * pt1 + 0.2 * pt2 + 0.4 * pt3)
            sample_points.append(0.4 * pt1 + 0.4 * pt2 + 0.2 * pt3)
        return sample_points

    def _possible_to_machine(self, bound, normal, tris) -> bool:
        centroid = (bound[0] + bound[1] + bound[2] + bound[3]) / 4
        points = np.array((bound[0], bound[1], bound[2], bound[3], centroid))
        return bool(np.any(geom.ray_triangle_min_t(points, normal, tris) != -np.inf))

    def _bound_inner(self):
        fe_list = self._face_filter(num_edges=0)
        tris = occ.triangles_from_faces(fe_list)
        for face in fe_list:
            normal = occ.normal_to_face_center(face)
            sample_pnts = self._sample_points_inside_face(face)
            edges = occ.list_edge(face)
            for apnt in sample_pnts:
                dist, p = occ.dist_point_to_edges(apnt, edges)
                if p is None:
                    continue
                p = np.array(p)
                if dist >= params.MIN_LEN / 2 + params.CLEARANCE:
                    dir_w = apnt - p
                    unit_dir_w = dir_w / np.linalg.norm(dir_w)
                    dir_h = np.cross(unit_dir_w, normal)
                    dist -= params.CLEARANCE
                    part_a = apnt - dist * unit_dir_w
                    part_b = dist * unit_dir_w + apnt
                    part_c = dist * dir_h
                    bound = np.array((part_a - part_c, part_c + part_a, part_c + part_b,
                                      part_b - part_c, -normal))
                    if not self._possible_to_machine(bound, normal, tris):
                        self.bounds.append(bound)

    def _rect_size(self, rect):
        return float(np.linalg.norm(rect[2] - rect[1])), float(np.linalg.norm(rect[0] - rect[1]))

    def _bound_1(self):
        concave = occ.concave_edges(self.shape)
        fe_list = self._face_filter(num_edges=1)
        tris = occ.triangles_from_faces(occ.list_face(self.shape))
        for face, edge in fe_list:
            if concave.Contains(edge):
                continue
            pts, _, _, et_map = occ.triangulation_from_face(face)
            segs = [[pts[a], pts[b]] for (a, b), t in et_map.items() if len(t) == 1]
            pts = np.asarray(pts, dtype=np.float64)
            normal = occ.normal_to_face_center(face)
            pnt1 = occ.first_vertex(edge)
            pnt2 = occ.last_vertex(edge)
            edge_dir = pnt2 - pnt1
            edge_len = float(np.linalg.norm(edge_dir))
            if edge_len < (2 * params.CLEARANCE) + params.MIN_LEN:
                continue
            edge_unit_dir = edge_dir / edge_len
            pnt1 = pnt1 + params.CLEARANCE * edge_unit_dir
            pnt2 = pnt2 - params.CLEARANCE * edge_unit_dir
            edge_len -= 2 * params.CLEARANCE
            edge_normal = geom.unit(np.cross(normal, edge_unit_dir))
            num_sample = int(edge_len / params.MIN_LEN)
            sample_pnts = [pnt1 + t * params.MIN_LEN * edge_unit_dir for t in range(num_sample)]
            sample_pnts.append(pnt2)
            sample_pnts = np.array(sample_pnts, dtype=np.float64)
            segs = np.array(segs, dtype=np.float64)
            inter_pnts = []
            for s in sample_pnts:
                for inter in sorted(geom.ray_segment_set_intersect(s, edge_normal, segs)):
                    if inter > 0:
                        inter_pnts.append(s + inter * edge_normal)
                        break
            if len(inter_pnts) != len(sample_pnts):
                raise RuntimeError("bound_1: a sample ray left the face without a boundary hit")
            pts = np.append(pts, inter_pnts, axis=0)
            inter_pnts = np.array(inter_pnts, dtype=np.float64)
            for i in range(sample_pnts.shape[0] - 1):
                vec1 = inter_pnts[i] - sample_pnts[i]
                for j in range(i + 1, sample_pnts.shape[0]):
                    vec2 = inter_pnts[j] - sample_pnts[j]
                    verts = np.array([sample_pnts[i] + vec1, sample_pnts[i], sample_pnts[j],
                                      sample_pnts[j] + vec2])
                    bound = geom.search_rect_inside_bound_1(verts, vec1, vec2, pts)
                    w, h = self._rect_size(bound)
                    if h >= params.MIN_LEN + params.CLEARANCE:
                        bound = self._shrink_bound_1(bound)
                        bound = np.append(bound, [-normal], axis=0)
                        if not self._possible_to_machine(bound, normal, tris):
                            self.bounds.append(bound)

    def _shrink_bound_1(self, bound):
        dir_h_1 = bound[0] - bound[1]
        len_h_1 = np.linalg.norm(dir_h_1)
        dir_h_2 = bound[3] - bound[2]
        len_h_2 = np.linalg.norm(dir_h_2)
        if len_h_1 <= len_h_2:
            dir_h = (params.CLEARANCE / len_h_1) * dir_h_1
            h_dif = dir_h_1 - dir_h_2
            bound[0] = bound[0] - dir_h
            bound[3] = bound[3] - dir_h + h_dif
        else:
            dir_h = (params.CLEARANCE / len_h_2) * dir_h_2
            h_dif = dir_h_2 - dir_h_1
            bound[0] = bound[0] - dir_h + h_dif
            bound[3] = bound[3] - dir_h
        return bound

    _shrink_bound_3 = _shrink_bound_1

    def _shrink_bound_2(self, bound):
        dir_w = bound[2] - bound[1]
        dir_h = bound[0] - bound[1]
        dir_w = (params.CLEARANCE / np.linalg.norm(dir_w)) * dir_w
        dir_h = (params.CLEARANCE / np.linalg.norm(dir_h)) * dir_h
        bound[0] = bound[0] - dir_h
        bound[2] = bound[2] - dir_w
        bound[3] = bound[3] - dir_h - dir_w
        return bound

    @staticmethod
    def _angle_between_edges(pnt0, pnt1, pnt2):
        vec_a = pnt1 - pnt0
        vec_b = pnt2 - pnt1
        angle_cos = np.dot(vec_a, vec_b) / np.linalg.norm(vec_a) / np.linalg.norm(vec_b)
        return np.arccos(angle_cos) * 180 / math.pi

    def _bound_2(self):
        concave = occ.concave_edges(self.shape)
        fe_list = self._face_filter(num_edges=2)
        tris = occ.triangles_from_faces(occ.list_face(self.shape))
        for face, edge1, edge2 in fe_list:
            if concave.Contains(edge1) or concave.Contains(edge2):
                continue
            pnt0 = occ.first_vertex(edge1)
            pnt1 = occ.first_vertex(edge2)
            pnt2 = occ.last_vertex(edge2)
            if self._angle_between_edges(pnt0, pnt1, pnt2) != 90.0:
                continue
            pts = np.asarray(occ.triangulation_from_face(face)[0], dtype=np.float64)
            vec0 = pnt0 - pnt1
            vec2 = pnt2 - pnt1
            verts = np.array([pnt1 + vec0, pnt1, pnt1 + vec2, pnt1 + vec0 + vec2])
            bound = geom.search_rect_inside_bound_2(verts, vec0, vec2, pts)
            w, h = self._rect_size(bound)
            if w >= params.MIN_LEN + params.CLEARANCE and h >= params.MIN_LEN + params.CLEARANCE:
                normal = occ.normal_to_face_center(face)
                bound = self._shrink_bound_2(bound)
                bound = np.append(bound, [-normal], axis=0)
                if not self._possible_to_machine(bound, normal, tris):
                    self.bounds.append(bound)

    def _bound_3(self):
        concave = occ.concave_edges(self.shape)
        fe_list = self._face_filter(num_edges=3)
        tris = occ.triangles_from_faces(occ.list_face(self.shape))
        for face, edge1, edge2, edge3 in fe_list:
            if concave.Contains(edge1) or concave.Contains(edge2) or concave.Contains(edge3):
                continue
            pts = np.asarray(occ.triangulation_from_face(face)[0], dtype=np.float64)
            vertices = np.array([occ.first_vertex(edge1), occ.first_vertex(edge2),
                                 occ.first_vertex(edge3), occ.last_vertex(edge3)])
            bound = geom.search_rect_inside_bound_3(vertices, pts)
            w, h = self._rect_size(bound)
            if w >= params.MIN_LEN and h >= params.MIN_LEN + params.CLEARANCE:
                normal = occ.normal_to_face_center(face)
                bound = self._shrink_bound_3(bound)
                bound = np.append(bound, [-normal], axis=0)
                if not self._possible_to_machine(bound, normal, tris):
                    self.bounds.append(bound)

    # ------------------------------------------------------------------ depth / shifter
    def _get_depth(self, bound, tris):
        if self.depth_type == "through":
            return max(self.ctx.stock)
        return self._depth_blind(bound, tris)

    def _depth_blind(self, bound, tris):
        thres = self.min_len + self.clearance
        points = geom.points_inside_rect(bound[0], bound[1], bound[2], bound[3], 0.2)
        dpt = geom.ray_triangle_min_t(points, bound[4], tris)
        dpt = dpt[dpt >= 0.0]
        if np.any(dpt + 1e-6 < thres):
            return -np.inf
        if not len(dpt):
            # the original's min() of an empty list raised, aborting the feature
            raise ValueError("blind depth: no ray hit the part")
        d_min = float(dpt.min())
        return self.rng.uniform(self.min_len, d_min - self.clearance)

    def _shifter(self, b):
        dir_w = b[2] - b[1]
        dir_h = b[0] - b[1]
        old_w = float(np.linalg.norm(dir_w))
        old_h = float(np.linalg.norm(dir_h))
        dir_w = dir_w / old_w
        dir_h = dir_h / old_h
        scale_w = self.rng.uniform(0.1, 1.0)
        scale_h = self.rng.uniform(0.1, 1.0)
        new_w = max(params.MIN_LEN, old_w * scale_w)
        new_h = max(params.MIN_LEN, old_h * scale_h)
        if self.shifter_type == 1:
            b[1] = b[1] + self.rng.uniform(0.0, old_w - new_w) * dir_w
        if self.shifter_type == 4:
            offset_w = self.rng.uniform(0.0, old_w - new_w)
            offset_h = self.rng.uniform(0.0, old_h - new_h)
            b[1] = b[1] + offset_w * dir_w + offset_h * dir_h
        b[0] = b[1] + new_h * dir_h
        if self.shifter_type != 3:
            b[2] = b[1] + new_w * dir_w
            b[3] = b[0] + new_w * dir_w
        else:
            b[3] = b[2] + new_h * dir_h
        return b

    # ------------------------------------------------------------------ apply
    def _add_sketch(self, bound):
        raise NotImplementedError(self.feat_type)

    def _apply_feature(self, feat_face, depth_dir):
        maker = BRepFeat_MakePrism()
        maker.Init(self.shape, feat_face, TopoDS_Face(), occ.gdir(depth_dir), 0, False)
        maker.Build()
        maker.Perform(float(np.linalg.norm(depth_dir)))
        shape = maker.Shape()
        return shape, maker

    def add_feature(self, bounds, find_bounds=True):
        """(labels, bounds, applied). A feature that cannot be placed, fails to build, or
        leaves anything but one valid solid returns the incoming labels unchanged."""
        unchanged = (self.labels, bounds, False)
        try:
            if find_bounds:
                self._get_bounds()
            else:
                self.bounds = bounds
            if len(self.bounds) < 1:
                return self.labels, self.bounds, False
            feat_face = None
            tris = occ.triangles_from_faces(occ.list_face(self.shape))
            self.rng.shuffle(self.bounds)
            try_cnt = 0
            bound_max = None
            while try_cnt < len(self.bounds):
                bound_max = self._shifter(self.rng.choice(self.bounds))
                depth = self._get_depth(bound_max, tris)
                if depth <= 0:
                    try_cnt += 1
                    continue
                feat_face = self._add_sketch(bound_max)
                try_cnt = len(self.bounds)
        except Exception as e:  # noqa: BLE001 — the original printed and skipped the feature
            self.ctx.log.append(f"{self.feat_type}: {type(e).__name__}: {e}")
            return unchanged
        if feat_face is None:
            return unchanged
        try:
            shape, maker = self._apply_feature(feat_face, bound_max[4] * depth)
        except Exception as e:  # noqa: BLE001 — a failed prism skips the feature
            self.ctx.log.append(f"{self.feat_type}: prism: {type(e).__name__}: {e}")
            return unchanged
        if not occ.is_valid_single_solid(shape):
            return unchanged
        labels = self.labels.after(maker, shape, params.FEAT_NAMES.index(self.feat_type),
                                   self.ctx.next_instance)
        self.ctx.next_instance += 1
        return labels, self.bounds, True


# ---------------------------------------------------------------------------- sketch helpers

def _circle_face(center, normal, radius):
    circ = gp_Circ(gp_Ax2(occ.pnt(center), occ.gdir(normal)), float(radius))
    edge = BRepBuilderAPI_MakeEdge(circ, 0., 2 * math.pi).Edge()
    return BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(edge).Wire()).Face()


def _wire_face(makers):
    wire = BRepBuilderAPI_MakeWire()
    for sm in makers:
        wire.Add(BRepBuilderAPI_MakeEdge(sm.Value()).Edge())
    return BRepBuilderAPI_MakeFace(wire.Wire()).Face()


def _rect_frame(bound):
    dir_w = bound[2] - bound[1]
    dir_h = bound[0] - bound[1]
    width = float(np.linalg.norm(dir_w))
    height = float(np.linalg.norm(dir_h))
    return dir_w / width, dir_h / height, width, height


def _circle_points(rng, bound, first_span, step_fn, n):
    dir_w, dir_h, width, height = _rect_frame(bound)
    normal = occ.gdir(np.cross(dir_w, dir_h))
    radius = min(width / 2, height / 2)
    center = (bound[0] + bound[1] + bound[2] + bound[3]) / 4
    circ = Geom_Circle(gp_Ax2(occ.pnt(center), normal), radius)
    ang = rng.uniform(0.0, first_span)
    out = [occ.as_list(circ.Value(ang))]
    for _ in range(n - 1):
        ang = step_fn(ang)
        out.append(occ.as_list(circ.Value(ang)))
    return out


def _triangle_step(rng):
    def step(a):
        a = a + rng.uniform(2 * math.pi / 3 - math.pi / 9, 2 * math.pi / 3 + math.pi / 9)
        return a - 2 * math.pi if a > 2 * math.pi else a
    return step


# ---------------------------------------------------------------------------- features

class _Polygon4(MachiningFeature):
    def _add_sketch(self, bound):
        return occ.face_polygon(bound[:4])


class RectangularPassage(_Polygon4):
    shifter_type, bound_type, depth_type, feat_type = 4, 4, "through", "rectangular_passage"


class RectangularPocket(_Polygon4):
    shifter_type, bound_type, depth_type, feat_type = 4, 4, "blind", "rectangular_pocket"


class RectangularThroughSlot(_Polygon4):
    shifter_type, bound_type, depth_type, feat_type = 1, 1, "through", "rectangular_through_slot"


class RectangularBlindSlot(_Polygon4):
    shifter_type, bound_type, depth_type, feat_type = 1, 1, "blind", "rectangular_blind_slot"


class RectangularThroughStep(_Polygon4):
    shifter_type, bound_type, depth_type, feat_type = 3, 3, "blind", "rectangular_through_step"


class RectangularBlindStep(_Polygon4):
    shifter_type, bound_type, depth_type, feat_type = 2, 2, "blind", "rectangular_blind_step"


class TriangularBlindStep(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 2, 2, "blind", "triangular_blind_step"

    def _add_sketch(self, bound):
        return occ.face_polygon([bound[0], bound[1], bound[2]])


class TriangularThroughSlot(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 1, 1, "through", "triangular_through_slot"

    def _add_sketch(self, bound):
        return occ.face_polygon([bound[1], bound[2], bound[0] + (bound[2] - bound[1]) / 2])


class SlantedThroughStep(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 3, 3, "blind", "slanted_through_step"

    def _add_sketch(self, bound):
        dir_l = bound[0] - bound[1]
        dir_r = bound[3] - bound[2]
        mark = [0, 1]
        self.rng.shuffle(mark)
        ratio = self.rng.uniform(0.3, 0.6)
        return occ.face_polygon([bound[0] - dir_l * mark[0] * ratio, bound[1], bound[2],
                                 bound[3] - dir_r * mark[1] * ratio])


class TwoSidesThroughStep(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 3, 3, "blind", "2sides_through_step"

    def _add_sketch(self, bound):
        dir_l = bound[0] - bound[1]
        dir_r = bound[3] - bound[2]
        ratio = self.rng.uniform(0.4, 0.8)
        return occ.face_polygon([bound[1] + dir_l * ratio, bound[1], bound[2],
                                 bound[2] + dir_r * ratio, (bound[0] + bound[3]) / 2])


class TriangularPassage(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 4, 4, "through", "triangular_passage"

    def _add_sketch(self, bound):
        return occ.face_polygon(_circle_points(self.rng, bound, 2 * math.pi / 3,
                                               _triangle_step(self.rng), 3))


class TriangularPocket(TriangularPassage):
    depth_type, feat_type = "blind", "triangular_pocket"


class SixSidesPassage(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 4, 4, "through", "6sides_passage"

    def _add_sketch(self, bound):
        return occ.face_polygon(_circle_points(self.rng, bound, math.pi / 3,
                                               lambda a: a + math.pi / 3, 6))


class SixSidesPocket(SixSidesPassage):
    depth_type, feat_type = "blind", "6sides_pocket"


class ThroughHole(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 4, 4, "through", "through_hole"

    def _add_sketch(self, bound):
        dir_w, dir_h, width, height = _rect_frame(bound)
        center = (bound[0] + bound[1] + bound[2] + bound[3]) / 4
        return _circle_face(center, np.cross(dir_w, dir_h), min(width / 2, height / 2))


class BlindHole(ThroughHole):
    depth_type, feat_type = "blind", "blind_hole"


class ORing(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 4, 4, "blind", "Oring"

    def _add_sketch(self, bound):
        dir_w, dir_h, width, height = _rect_frame(bound)
        normal = occ.gdir(np.cross(dir_w, dir_h))
        outer_r = min(width / 2, height / 2)
        center = occ.pnt((bound[0] + bound[1] + bound[2] + bound[3]) / 4)
        inner_r = self.rng.uniform(outer_r / 3, outer_r - 0.2)
        edge = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(center, normal), outer_r), 0., 2 * math.pi).Edge()
        outer_wire = BRepBuilderAPI_MakeWire(edge).Wire()
        normal.Reverse()
        edge = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(center, normal), inner_r), 0., 2 * math.pi).Edge()
        inner_wire = BRepBuilderAPI_MakeWire(edge).Wire()
        face_maker = BRepBuilderAPI_MakeFace(outer_wire)
        face_maker.Add(inner_wire)
        return face_maker.Face()


class CircularThroughSlot(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 1, 1, "through", "circular_through_slot"

    def _add_sketch(self, bound):
        edge_dir = bound[2] - bound[1]
        width = float(np.linalg.norm(edge_dir))
        edge_dir = edge_dir / width
        height = float(np.linalg.norm(bound[0] - bound[1]))
        radius = min(width / 2, height)
        center = (bound[1] + bound[2]) / 2
        if radius < width / 2:
            pnt1 = occ.pnt(center - edge_dir * radius)
            pnt2 = occ.pnt(center + edge_dir * radius)
        else:
            pnt1 = occ.pnt(bound[1])
            pnt2 = occ.pnt(bound[2])
        normal = occ.gdir(np.cross(edge_dir, bound[0] - bound[1]))
        circ = gp_Circ(gp_Ax2(occ.pnt(center), normal), radius)
        edge1 = BRepBuilderAPI_MakeEdge(GC_MakeSegment(pnt1, pnt2).Value()).Edge()
        edge2 = BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(circ, pnt2, pnt1, True).Value()).Edge()
        return BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(edge1, edge2).Wire()).Face()


class CircularBlindStep(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 2, 2, "blind", "circular_blind_step"

    def _add_sketch(self, bound):
        vec0 = bound[0] - bound[1]
        vec2 = bound[2] - bound[1]
        radius = min(float(np.linalg.norm(vec2)), float(np.linalg.norm(vec0)))
        vec0 = geom.unit(vec0)
        vec2 = geom.unit(vec2)
        pt0 = occ.pnt(bound[1] + vec0 * radius)
        pt1 = occ.pnt(bound[1])
        pt2 = occ.pnt(bound[1] + vec2 * radius)
        cir = gp_Circ(gp_Ax2(pt1, occ.gdir(np.cross(vec2, vec0))), radius)
        return _wire_face([GC_MakeSegment(pt0, pt1), GC_MakeSegment(pt1, pt2),
                           GC_MakeArcOfCircle(cir, pt2, pt0, True)])


class CircularEndPocket(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 4, 4, "blind", "circular_end_pocket"

    def _add_sketch(self, bound):
        width = float(np.linalg.norm(bound[2] - bound[1]))
        height = float(np.linalg.norm(bound[0] - bound[1]))
        pt0, pt1, pt2 = bound[0], bound[1], bound[2]
        if width < height:
            pt0, pt1, pt2 = bound[1], bound[2], bound[3]
            width, height = height, width
        radius = min(height / 2, width / 2 - 0.5)
        dir_w = (pt2 - pt1) / width
        dir_h = (pt0 - pt1) / height
        pt1 = pt1 + dir_w * radius
        pt2 = pt2 - dir_w * radius
        pt3 = pt2 + dir_h * 2 * radius
        pt0 = pt1 + dir_h * 2 * radius
        c01 = (pt1 + pt0) / 2
        c23 = (pt2 + pt3) / 2
        normal = occ.gdir(np.cross(dir_w, dir_h))
        cir01 = gp_Circ(gp_Ax2(occ.pnt(c01), normal), radius)
        cir23 = gp_Circ(gp_Ax2(occ.pnt(c23), normal), radius)
        p0, p1, p2, p3 = (occ.pnt(p) for p in (pt0, pt1, pt2, pt3))
        return _wire_face([GC_MakeArcOfCircle(cir01, p0, p1, True), GC_MakeSegment(p1, p2),
                           GC_MakeArcOfCircle(cir23, p2, p3, True), GC_MakeSegment(p3, p0)])


class HCircularEndBlindSlot(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 1, 1, "blind", "h_circular_end_blind_slot"

    def _add_sketch(self, bound):
        dir_w, dir_h, width, height = _rect_frame(bound)
        normal = np.cross(dir_w, dir_h)
        rect_h = min(height, width / 2 - 0.5)
        pt1 = bound[1]
        pt2 = bound[2]
        pt3 = pt2 - dir_w * rect_h + dir_h * rect_h
        pt4 = pt1 + dir_w * rect_h + dir_h * rect_h
        circ1 = gp_Circ(gp_Ax2(occ.pnt(pt1 + dir_w * rect_h), occ.gdir(normal)), rect_h)
        circ2 = gp_Circ(gp_Ax2(occ.pnt(pt2 - dir_w * rect_h), occ.gdir(normal)), rect_h)
        p1, p2, p3, p4 = (occ.pnt(p) for p in (pt1, pt2, pt3, pt4))
        return _wire_face([GC_MakeSegment(p1, p2), GC_MakeArcOfCircle(circ2, p2, p3, True),
                           GC_MakeSegment(p3, p4), GC_MakeArcOfCircle(circ1, p4, p1, True)])


class VCircularEndBlindSlot(MachiningFeature):
    shifter_type, bound_type, depth_type, feat_type = 1, 1, "blind", "v_circular_end_blind_slot"

    def _add_sketch(self, bound):
        dir_w, dir_h, width, height = _rect_frame(bound)
        if height - width / 2 > 1.0:
            radius = width / 2
        else:
            radius = self.rng.uniform(0.5, height / 2)
        offset = width / 2 - radius
        normal = np.cross(dir_w, dir_h)
        pt1 = bound[1] + dir_w * offset
        pt2 = pt1 + dir_w * 2 * radius
        pt3 = pt2 + dir_h * (height - radius)
        pt4 = pt1 + dir_h * (height - radius)
        center = pt4 + dir_w * radius
        circ = gp_Circ(gp_Ax2(occ.pnt(center), occ.gdir(normal)), radius)
        p1, p2, p3, p4 = (occ.pnt(p) for p in (pt1, pt2, pt3, pt4))
        return _wire_face([GC_MakeSegment(p1, p2), GC_MakeSegment(p2, p3),
                           GC_MakeArcOfCircle(circ, p3, p4, True), GC_MakeSegment(p4, p1)])


# ---------------------------------------------------------------------------- transitions

class Chamfer:
    """Chamfer one random edge (Features/chamfer.py)."""
    feat_type = "chamfer"

    def __init__(self, ctx: Ctx, labels: occ.FaceLabels, edges: list):
        self.ctx, self.labels, self.edges = ctx, labels, edges

    def add_feature(self):
        shape = self.labels.shape
        while self.edges:
            edge = self.ctx.rng.choice(self.edges)
            for depth in (self.ctx.rng.uniform(params.CHAMFER_DEPTH_MIN, params.CHAMFER_DEPTH_MAX),
                          params.CHAMFER_DEPTH_MIN):
                maker = BRepFilletAPI_MakeChamfer(shape)
                try:
                    maker.Add(depth, edge)
                    new_shape = maker.Shape()
                except Exception:  # noqa: BLE001 — retried at the minimum depth, then dropped
                    continue
                self.edges.remove(edge)
                return self._finish(maker, new_shape)
            self.edges.remove(edge)
        return self.labels, self.edges, False

    def _finish(self, maker, new_shape):
        if not occ.is_valid_single_solid(new_shape):
            return self.labels, self.edges, False
        labels = self.labels.after(maker, new_shape, params.FEAT_NAMES.index(self.feat_type),
                                   self.ctx.next_instance)
        self.ctx.next_instance += 1
        return labels, self.edges, True


class Round(Chamfer):
    """Fillet one random edge (Features/round.py)."""
    feat_type = "round"

    def add_feature(self):
        shape = self.labels.shape
        self.edges[:] = [e for e in self.edges if occ.edge_length(e) >= params.MIN_LEN]
        while self.edges:
            edge = self.ctx.rng.choice(self.edges)
            max_radius = min(occ.edge_length(edge) / 10, params.ROUND_RADIUS_MAX)
            radius = self.ctx.rng.uniform(params.ROUND_RADIUS_MIN, max_radius)
            # the original kept adding to one maker, so a failed edge stayed in every later try
            maker = BRepFilletAPI_MakeFillet(shape)
            try:
                maker.Add(radius, edge)
                new_shape = maker.Shape()
            except Exception:  # noqa: BLE001 — this edge cannot be filleted; try another
                self.edges.remove(edge)
                continue
            self.edges.remove(edge)
            return self._finish(maker, new_shape)
        return self.labels, self.edges, False


FEATURE_CLASSES = {c.feat_type: c for c in (
    Chamfer, ThroughHole, TriangularPassage, RectangularPassage, SixSidesPassage,
    TriangularThroughSlot, RectangularThroughSlot, CircularThroughSlot, RectangularThroughStep,
    TwoSidesThroughStep, SlantedThroughStep, ORing, BlindHole, TriangularPocket, RectangularPocket,
    SixSidesPocket, CircularEndPocket, RectangularBlindSlot, VCircularEndBlindSlot,
    HCircularEndBlindSlot, TriangularBlindStep, CircularBlindStep, RectangularBlindStep, Round)}
