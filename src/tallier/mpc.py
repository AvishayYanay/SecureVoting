# Copyright (C) 2021-2022 Arthur Zamarin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from functools import lru_cache
from itertools import combinations, count, repeat
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from random import randint
import operator
import asyncio
import math

from mytypes import Election
import utils


class TallierConn:
    async def close(self):
        raise NotImplementedError()

    async def read(self, msgid: int) -> int:
        raise NotImplementedError()

    async def write(self, msgid: int, val: int):
        raise NotImplementedError()

    async def receive_loop(self):
        raise NotImplementedError()

TallierConnFactory = Callable[[asyncio.StreamReader, asyncio.StreamWriter], TallierConn]

class MPC:
    def __init__(self, election: Election, talliers: Tuple[TallierConn, ...]):
        self.election = election
        self.D = len(talliers)
        self.p = election.p

        self.vandermond_first_row = utils.inverse([[pow(i, j, self.p) for j in range(self.D)] for i in range(1, self.D + 1)], self.p)[0]
        self.gen_shamir = lambda val: utils.clean_gen_shamir(val, self.D, (self.D + 1) // 2, self.p)
        self.block_size = int(2 * math.ceil(math.sqrt(math.ceil(math.log2(self.p)))) ** 2)

        self.collectors = [asyncio.create_task(tallier.receive_loop()) for tallier in talliers.values()]
        self.talliers = talliers
        self.borda_random = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kargs):
        for collector in self.collectors:
            collector.cancel()
        await asyncio.gather(*(v.close() for v in self.talliers))
        if config.debug:
            print('Closed all')

    async def exchange(self, msgid: int, values: Tuple[int, ...]) -> List[int]:
        async def single_exchange(tallier: Optional[TallierConn], value: int) -> int:
            if not tallier:
                return value
            await tallier.write(msgid, value)
            return await tallier.read(msgid)
        return list(await asyncio.gather(*map(single_exchange, self.talliers, values)))

    async def multiply(self, msgid: int, a: int, b: int) -> int:
        results = await self.exchange(msgid, self.gen_shamir((a * b) % self.p))
        return sum(map(operator.mul, self.vandermond_first_row, results)) % self.p

    async def resolve(self, msgid: int, a: int) -> int:
        results = await self.exchange(msgid, tuple(a for _ in range(self.D)))
        return utils.resolve(results, self.p)

    async def random_number(self, msgid: int) -> int:  # Joint Random Number Sharing
        r_i = self.gen_shamir(randint(0, self.p - 1))
        return sum(await self.exchange(msgid, r_i)) % self.p

    async def random_bit(self, msgid: int) -> int:  # Joint Random Bit Sharing
        while True:
            r = await self.random_number(msgid)
            r2 = await self.resolve(msgid, await self.multiply(msgid, r, r))
            if r2 != 0:
                root_inv = pow(utils.modular_sqrt(r2, self.p), -1, self.p)
                return ((r * root_inv + 1) * pow(2, -1, self.p)) % self.p

    @lru_cache
    def _fan_in_or_coefficients(self, length):
        f_l = [(1, 0)] + [(i + 2, 1) for i in range(length)]
        return utils.lagrange_polynomial(f_l, self.p)

    async def fan_in_or(self, msgid: int, a_i: List[int]) -> int:  # Unbounded Fan-In Or
        assert len(a_i) > 0
        A = (1 + sum(a_i)) % self.p
        alpha_i = self._fan_in_or_coefficients(len(a_i))

        res = alpha_i[0] + alpha_i[1] * A
        mul_A = A
        for i in range(1, len(a_i)):
            res += alpha_i[i + 1] * (mul_A := await self.multiply(msgid, A, mul_A))
        return res % self.p

    async def prefix_or(self, msgid: int, a_i: List[int]) -> List[int]:  # Prefix-Or
        async def calc_mul(msgbase: int, first: int, seconds: Iterable[int]) -> Tuple[int]:
            return await asyncio.gather(*map(self.multiply, count(msgbase), repeat(first), seconds))
        assert len(a_i) > 0
        orig_len = len(a_i)
        lam = math.ceil(math.sqrt(len(a_i)))
        a_i = a_i + [0] * (lam ** 2 - len(a_i))
        a_ij = [a_i[i:i + lam] for i in range(0, len(a_i), lam)]
        x_i = await asyncio.gather(*map(self.fan_in_or, count(msgid, 2 * lam), a_ij))
        y_i = await asyncio.gather(*(self.fan_in_or(msgid + 2 * i * lam, x_i[:i + 1]) for i in range(lam)))
        f_i = [y_i[0]] + [y_i[i] - y_i[i - 1] for i in range(1, lam)]
        g_ij = await asyncio.gather(*map(calc_mul, count(msgid, lam), f_i, a_ij))
        c_j = [sum(g_ij[i][j] for i in range(lam)) % self.p for j in range(lam)]
        h_j = await asyncio.gather(*(self.fan_in_or(msgid + 2 * j * lam, c_j[:j + 1]) for j in range(lam)))
        s_ij = await asyncio.gather(*map(calc_mul, count(msgid, lam), f_i, repeat(h_j)))
        return [(s + y - f) % self.p for s_ij_i, y, f in zip(s_ij, y_i, f_i) for s in s_ij_i][:orig_len]

    async def xor(self, msgid: int, a_i: List[int], b_i: List[int]) -> List[int]:
        assert len(a_i) == len(b_i)
        c_i = await asyncio.gather(*map(self.multiply, count(msgid), a_i, b_i))
        return [(a + b - 2 * c) for a, b, c in zip(a_i, b_i, c_i)]

    async def less_bitwise(self, msgid: int, a_i: List[int], b_i: List[int]) -> int:  # Bitwise Less-Than
        assert len(a_i) == len(b_i) > 0
        c_i = await self.xor(msgid, a_i, b_i)
        c_i.reverse()
        d_i = await self.prefix_or(msgid, c_i)
        d_i.reverse()
        e_i = [(d_i[i] - d_i[i + 1]) % self.p for i in range(len(a_i) - 1)] + [d_i[-1]]
        h_i = await asyncio.gather(*map(self.multiply, count(msgid), e_i, b_i))
        return sum(h_i) % self.p

    async def random_number_bits(self, msgid: int) -> Tuple[int]:  # Joint Random Number Bitwise-Sharing
        while True:
            bits_count = math.ceil(math.log2(self.p))
            r_i = await asyncio.gather(*map(self.random_bit, range(msgid, msgid + bits_count)))
            p_i = [int(digit) for digit in reversed(bin(p)[2:])]
            check_bit = await self.resolve(msgid, await self.less_bitwise(msgid, r_i, p_i))
            if check_bit == 1:
                return r_i

    async def is_odd(self, msgid: int, x: int) -> int:  # LSB of number
        r_i = await self.random_number_bits(msgid)
        r = sum(bit * 2 ** idx for idx, bit in enumerate(r_i)) % self.p
        c = await self.resolve(msgid, (x + r) % self.p)
        d = r_i[0] if c % 2 == 0 else (1 - r_i[0]) % self.p
        c_i = [int(digit) for digit in reversed(bin(c)[2:])]
        c_i += [0] * (len(r_i) - len(c_i))
        e = await self.less_bitwise(msgid, c_i, r_i)
        return (e + d - 2 * await self.multiply(msgid, e, d)) % self.p

    async def less_middle(self, msgid: int, a: int) -> int:  # Is less than half p
        return (1 - await self.is_odd(msgid, (2 * a) % self.p)) % self.p

    async def less(self, msgid: int, a: int, b: int) -> int:  # Comparison
        w, x, y = await asyncio.gather(self.less_middle(msgid + 0, a),
                                       self.less_middle(msgid + self.block_size, b),
                                       self.less_middle(msgid + self.block_size * 2, (a - b) % self.p))
        c = await self.multiply(msgid, x, y)
        d = (x + y - c) % self.p
        return (await self.multiply(msgid, w, (d - c) % self.p) + 1 - d) % self.p

    async def max(self, msgbase: int, votes: List[int]) -> int:
        async def max_idx(msgid: int, a: Tuple[int, int], b: Tuple[int, int]) -> Tuple[int, int]:
            c = await self.less(msgid, a[1], b[1])
            a = await asyncio.gather(self.multiply(msgid, c, b[1]),
                                     self.multiply(msgid + 1, c, b[0]),
                                     self.multiply(msgid + 2, (1 - c) % self.p, a[1]),
                                     self.multiply(msgid + 3, (1 - c) % self.p, a[0]))
            return (a[1] + a[3]) % self.p, (a[0] + a[2]) % self.p
        if len(votes) == 1:
            return 0
        votes_idx = list(enumerate(votes))
        while len(votes_idx) > 1:
            votes_idx = await asyncio.gather(*(map(max_idx, count(msgbase, 3 * self.block_size), votes_idx[::2], votes_idx[1::2]))) + votes_idx[len(votes_idx)^1:]
        return await self.resolve(msgbase, votes_idx[0][0])

    async def validate_approval(self, msgbase: int, votes: [int]):
        a_i = await asyncio.gather(*(self.multiply(msgid, a, (1 - a) % self.p) for msgid, a in enumerate(votes, start=msgbase)))
        a_i = await asyncio.gather(*map(self.resolve, count(msgbase), a_i))
        return all((a == 0 for a in a_i))

    async def validate_plurality(self, msgbase: int, votes: [int]):
        s, f = await asyncio.gather(self.resolve(msgbase, sum(votes) % self.p),
                                    self.validate_approval(msgbase + 1, votes))
        return s == 1 and f

    async def validate_veto(self, msgbase: int, votes: [int]):
        s, f = await asyncio.gather(self.resolve(msgbase, sum(votes) % self.p),
                                    self.validate_approval(msgbase + 1, votes))
        return s == self.M - 1 and f

    async def validate_range(self, msgbase: int, votes: [int], max_value: int):
        async def check_range(msgid: int, vote: int):
            mul = vote
            for i in range(max_value):
                mul = await self.multiply(msgid, mul, (i + 1 - vote) % self.p)
            return 0 == await self.resolve(msgid, mul)
        return all(await asyncio.gather(*map(check_range, count(msgbase), votes)))

    async def validate_borda(self, msgbase: int, votes: [int]):
        async def check_pair(msgid, pair: [int, int]) -> bool:
            rnd = await self.random_number(msgid)
            mul = await self.multiply(msgid, rnd, (pair[0] - pair[1]) % self.p)
            return 0 != await self.resolve(msgid, mul)

        async def two_stage_permute(msgid: int):
            return (all(await asyncio.gather(*map(check_pair, count(msgid), combinations(votes, 2)))) or
                    all(await asyncio.gather(*map(check_pair, count(msgid), combinations(votes, 2)))))

        return all(await asyncio.gather(self.validate_range(msgbase, votes, self.M - 1),
                                        two_stage_permute(msgbase + self.M)))

    async def validate(self, msgid: int, votes: [int]) -> bool:
        if config.selected_vote_system == config.VoteSystem.PLURALITY:
            return await self.validate_plurality(msgid, votes)
        elif config.selected_vote_system == config.VoteSystem.RANGE:
            return await self.validate_range(msgid, votes, config.L)
        elif config.selected_vote_system == config.VoteSystem.APPROVAL:
            return await self.validate_approval(msgid, votes)
        elif config.selected_vote_system == config.VoteSystem.VETO:
            return await self.validate_veto(msgid, votes)
        elif config.selected_vote_system == config.VoteSystem.BORDA:
            return await self.validate_borda(msgid, votes)
        else:
            return True

    @staticmethod
    def validate_blocksize() -> int:
        if config.selected_vote_system == config.VoteSystem.PLURALITY:
            return config.M + 1
        elif config.selected_vote_system == config.VoteSystem.RANGE:
            return config.M
        elif config.selected_vote_system == config.VoteSystem.APPROVAL:
            return config.M
        elif config.selected_vote_system == config.VoteSystem.VETO:
            return config.M + 1
        elif config.selected_vote_system == config.VoteSystem.BORDA:
            return (config.M * (config.M + 1)) // 2
        else:
            return 0