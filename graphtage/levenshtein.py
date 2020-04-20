import itertools
import logging
from typing import Iterator, List, Optional, Sequence, Tuple

import numpy as np

from .bounds import Bounded, Range
from .edits import Insert, Match, Remove
from .fibonacci import FibonacciHeap
from .printer import DEFAULT_PRINTER
from .sequences import SequenceEdit
from .tree import Edit, TreeNode


log = logging.getLogger(__name__)


def levenshtein_distance(s: str, t: str) -> int:
    """Canonical implementation of the Levenshtein distance metric"""
    rows = len(s) + 1
    cols = len(t) + 1
    dist: List[List[int]] = [[0] * cols for _ in range(rows)]

    for i in range(1, rows):
        dist[i][0] = i

    for i in range(1, cols):
        dist[0][i] = i

    col = row = 0
    for col in range(1, cols):
        for row in range(1, rows):
            if s[row - 1] == t[col - 1]:
                cost = 0
            else:
                cost = 1
            dist[row][col] = min(dist[row - 1][col] + 1,
                                 dist[row][col - 1] + 1,
                                 dist[row - 1][col - 1] + cost)

    return dist[row][col]


class EditDistance(SequenceEdit):
    def __init__(
            self,
            from_node: TreeNode,
            to_node: TreeNode,
            from_seq: Sequence[TreeNode],
            to_seq: Sequence[TreeNode],
            insert_remove_penalty: int = 1,
    ):
        self.penalty: int = insert_remove_penalty
        # Optimization: See if the sequences trivially share a common prefix or suffix.
        # If so, this will quadratically reduce the size of the Levenshtein matrix
        self.shared_prefix: List[Tuple[TreeNode, TreeNode]] = []
        for fn, tn in zip(from_seq, to_seq):
            if fn == tn:
                self.shared_prefix.append((fn, tn))
            else:
                break
        self.reversed_shared_suffix: List[Tuple[TreeNode, TreeNode]] = []
        for fn, tn in zip(
                reversed(from_seq[len(self.shared_prefix):]),
                reversed(to_seq[len(self.shared_prefix):])
        ):
            if fn == tn:
                self.reversed_shared_suffix.append((fn, tn))
            else:
                break
        self.reversed_shared_suffix = self.reversed_shared_suffix
        self.from_seq: Sequence[TreeNode] = from_seq[
                                                len(self.shared_prefix):len(from_seq)-len(self.reversed_shared_suffix)
                                            ]
        self.to_seq: Sequence[TreeNode] = to_seq[
                                            len(self.shared_prefix):len(to_seq)-len(self.reversed_shared_suffix)
                                          ]
        log.debug(f"Levenshtein len(shared prefix)={len(self.shared_prefix)}, len(shared suffix)={len(self.reversed_shared_suffix)}, len(from_seq)={len(self.from_seq)}, len(to_seq)={len(self.to_seq)}")
        constant_cost = 0
        if len(from_seq) != len(to_seq):
            sizes: FibonacciHeap[TreeNode, int] = FibonacciHeap(key=lambda node: node.total_size)
            if len(from_seq) < len(to_seq):
                smaller, larger = from_seq, to_seq
            else:
                larger, smaller = from_seq, to_seq
            for node in larger:
                sizes.push(node)
            for _ in range(len(larger) - len(smaller)):
                constant_cost += sizes.pop().total_size
        cost_upper_bound = sum(node.total_size for node in from_seq) + sum(node.total_size for node in to_seq)
        self.edit_matrix: List[List[Optional[Edit]]] = [
            [None] * (len(self.from_seq) + 1) for _ in range(len(self.to_seq) + 1)
        ]
        self.path_costs = np.full((len(self.to_seq) + 1, len(self.from_seq) + 1), 0, dtype=np.uint16)
        self.costs = np.full((len(self.to_seq) + 1, len(self.from_seq) + 1), 0, dtype=np.uint64)
        self._fringe_row: int = -1
        self._fringe_col: int = 0
        super().__init__(
            from_node=from_node,
            to_node=to_node,
            constant_cost=constant_cost,
            cost_upper_bound=cost_upper_bound
        )
        self.__edits: Optional[List[Edit]] = None

    def _add_node(self, row: int, col: int) -> bool:
        if self.edit_matrix[row][col] is not None or col > len(self.from_seq) or row > len(self.to_seq):
            return False
        if row == 0 and col == 0:
            edit = None
        elif row == 0:
            edit = Remove(to_remove=self.from_seq[col - 1], remove_from=self.from_node, penalty=self.penalty)
            self.costs[0][col] = self.costs[0][col-1] + edit.bounds().upper_bound
            self.path_costs[0][col] = self.path_costs[0][col - 1] + 1
        elif col == 0:
            edit = Insert(to_insert=self.to_seq[row - 1], insert_into=self.from_node, penalty=self.penalty)
            self.costs[row][0] = self.costs[row - 1][0] + edit.bounds().upper_bound
            self.path_costs[row][0] = self.path_costs[row - 1][0] + 1
        else:
            edit = self.from_seq[col-1].edits(self.to_seq[row-1])
        self.edit_matrix[row][col] = edit
        return True

    def _fringe_diagonal(self) -> Iterator[Tuple[int, int]]:
        row, col = self._fringe_row, self._fringe_col
        while row >= 0 and col <= len(self.from_seq):
            yield row, col
            row -= 1
            col += 1

    def _next_fringe(self) -> bool:
        if self.is_complete():
            return False
        self._fringe_row += 1
        if self._fringe_row >= len(self.to_seq) + 1:
            self._fringe_row = len(self.to_seq)
            self._fringe_col += 1
        for row, col in self._fringe_diagonal():
            self._add_node(row, col)
        if self._fringe_col >= len(self.from_seq):
            if self._fringe_row < len(self.to_seq):
                # This is an edge case when the string we are matching from is shorter than the one we are matching to
                return True
            return False
        else:
            return True

    def is_complete(self) -> bool:
        return self.edit_matrix is None or self.edit_matrix[-1][-1] is not None

    def _best_match(self, row: int, col: int) -> Tuple[int, int, Edit]:
        if row == 0:
            assert col > 0
            return 0, col - 1, self.edit_matrix[0][col]
        elif col == 0:
            assert row > 0
            return row - 1, col, self.edit_matrix[row][0]
        else:
            dcost = (self.costs[row - 1][col - 1], self.path_costs[row - 1][col - 1])
            lcost = (self.costs[row][col - 1], self.path_costs[row][col - 1])
            ucost = (self.costs[row - 1][col], self.path_costs[row - 1][col])
            if dcost <= lcost and dcost <= ucost and \
                    self.edit_matrix[row][col].bounds() < self.edit_matrix[row][0].bounds() and \
                    self.edit_matrix[row][col].bounds() < self.edit_matrix[0][col].bounds():
                brow, bcol, edit = row - 1, col - 1, self.edit_matrix[row][col]
            elif ucost <= dcost:
                brow, bcol, edit = row - 1, col, self.edit_matrix[row][0]
            else:
                brow, bcol, edit = row, col - 1, self.edit_matrix[0][col]
            self.path_costs[row][col] = self.path_costs[brow][bcol] + 1
            self.costs[row][col] = self.costs[brow][bcol] + edit.bounds().upper_bound
            return brow, bcol, edit

    def tighten_bounds(self) -> bool:
        if not self.from_seq and not self.to_seq:
            return False
        elif self.edit_matrix is None:
            # This means we are already fully tightened and deleted the interstitial datastructures to save memory
            return False
        elif self.is_complete() and not self.edit_matrix[-1][-1].bounds().definitive():
            return self.edit_matrix[-1][-1].tighten_bounds()
        # We are still building the matrix
        initial_bounds: Range = self.bounds()
        while True:
            first_fringe = self._fringe_row < 0

            # Tighten the entire fringe diagonal until every node in it is definitive
            if not self._next_fringe():
                assert self.is_complete()
                if not self.edit_matrix[-1][-1].bounds().definitive():
                    ret = self.tighten_bounds()
                else:
                    ret = False
                if not ret:
                    self._cleanup()
                return ret

            if not first_fringe:
                if DEFAULT_PRINTER.quiet:
                    fringe_ranges = {}
                    fringe_total = 0
                else:
                    fringe_ranges = {
                        (row, col): self.edit_matrix[row][col].bounds().upper_bound - self.edit_matrix[row][col].bounds().lower_bound
                        for row, col in self._fringe_diagonal()
                    }
                    fringe_total = sum(fringe_ranges.values())

                with DEFAULT_PRINTER.tqdm(
                        total=fringe_total,
                        initial=fringe_total,
                        desc=f"Tightening Fringe Diagonal {self._fringe_row + self._fringe_col}",
                        disable=fringe_total <= 0,
                        leave=False
                ) as t:
                    for row, col in self._fringe_diagonal():
                        while self.edit_matrix[row][col].tighten_bounds():
                            if fringe_total:
                                new_bounds = self.edit_matrix[row][col].bounds()
                                new_range = new_bounds.upper_bound - new_bounds.lower_bound
                                t.update(fringe_ranges[(row, col)] - new_range)
                                fringe_ranges[(row, col)] = new_range
                        assert self.edit_matrix[row][col].bounds().definitive()
                        # Call self._best_match because it sets self.path_costs and self.costs for this cell
                        _, _, _ = self._best_match(row, col)

            if self.bounds().upper_bound < initial_bounds.upper_bound or \
                    self.bounds().lower_bound > initial_bounds.lower_bound:
                return True

    def bounds(self) -> Range:
        if self.is_complete():
            cost = int(self.costs[len(self.to_seq)][len(self.from_seq)])
            return Range(cost, cost)
        else:
            base_bounds: Range = super().bounds()
            if self._fringe_row <= 0:
                return base_bounds
            return Range(
                max(base_bounds.lower_bound, min(
                    int(self.costs[row][col]) for row, col in self._fringe_diagonal()
                )),
                base_bounds.upper_bound
            )

    def _cleanup(self):
        if self.bounds().definitive() and self.edit_matrix is not None:
            if self.__edits is None:
                self.edits()
            assert self.__edits is not None
            # we don't need the matrix anymore, so save memory by wiping it out
            self.edit_matrix = None
            self.path_costs = None
            # We only need the last cell in the costs matrix, so switch to using a dict to clean up the others:
            self.costs = {len(self.to_seq): {len(self.from_seq): self.costs[len(self.to_seq)][len(self.from_seq)]}}

    def edits(self) -> Iterator[Edit]:
        if self.__edits is None:
            reversed_suffix: List[Edit] = [
                Match(from_node, to_node, 0) for from_node, to_node in self.reversed_shared_suffix
            ]
            if self.to_seq or self.from_seq:
                while not self.is_complete() and self.tighten_bounds():
                    pass
                assert self.is_complete()
                if self.__edits is None:
                    assert len(self.edit_matrix) == len(self.to_seq) + 1
                    assert len(self.edit_matrix[0]) == len(self.from_seq) + 1
                    row, col = len(self.to_seq), len(self.from_seq)
                    while row > 0 or col > 0:
                        prev_row, prev_col, edit = self._best_match(row, col)
                        reversed_suffix.append(edit)
                        row, col = prev_row, prev_col
                    self.__edits = reversed_suffix
            else:
                self.__edits = reversed_suffix
            self._cleanup()
        return itertools.chain(
            (Match(from_node, to_node, 0) for from_node, to_node in self.shared_prefix),
            reversed(self.__edits)
        )
