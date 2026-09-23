"""Монотонное выравнивание двух последовательностей пунктов динамическим программированием.

Идея из выравнивания предложений в машинном переводе (Vecalign): порядок в редакциях
в основном сохранен, поэтому ищем путь через матрицу близости, разрешая связи
1:1, 1:2 и 2:1 (слияние и разделение пунктов) и разрывы (пункт без пары).

Стоимость связи = близость минус порог tau. Связь берется, только если она лучше разрыва,
то есть близость выше порога. Слияние 1:2 конкурирует с "1:1 плюс разрыв" честно:
сравнивается близость к склейке двух пунктов против близости к одному.

Перенос пункта в другой раздел ломает монотонность и выпадает как разрыв с обеих сторон.
Это не ошибка, это сигнал: такие разрывы потом проверяются глобальным поиском.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .similarity import Sim


@dataclass
class Link:
    a: list[int]  # индексы в последовательности A (до)
    b: list[int]  # индексы в последовательности B (после)
    sim: float


@dataclass
class Alignment:
    links: list[Link]
    gaps_a: list[int]
    gaps_b: list[int]


def align(texts_a: list[str], texts_b: list[str], sim: Sim, tau: float = 0.45) -> Alignment:
    n, m = len(texts_a), len(texts_b)
    S11 = sim.matrix(texts_a, texts_b)

    concat_a = [texts_a[i] + " " + texts_a[i + 1] for i in range(n - 1)]
    concat_b = [texts_b[j] + " " + texts_b[j + 1] for j in range(m - 1)]
    S21 = sim.matrix(concat_a, texts_b) if concat_a else np.zeros((0, m))  # (a_i+a_i+1) -> b_j
    S12 = sim.matrix(texts_a, concat_b) if concat_b else np.zeros((n, 0))  # a_i -> (b_j+b_j+1)

    NEG = -1e9
    dp = np.full((n + 1, m + 1), NEG)
    back = np.zeros((n + 1, m + 1), dtype=np.int8)  # 1: gapA, 2: gapB, 3: 1:1, 4: 1:2, 5: 2:1
    dp[0, 0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best, arg = NEG, 0
            if i > 0 and dp[i - 1, j] > best:
                best, arg = dp[i - 1, j], 1
            if j > 0 and dp[i, j - 1] > best:
                best, arg = dp[i, j - 1], 2
            if i > 0 and j > 0:
                v = dp[i - 1, j - 1] + S11[i - 1, j - 1] - tau
                if v > best:
                    best, arg = v, 3
            if i > 0 and j > 1:
                v = dp[i - 1, j - 2] + S12[i - 1, j - 2] - tau
                if v > best:
                    best, arg = v, 4
            if i > 1 and j > 0:
                v = dp[i - 2, j - 1] + S21[i - 2, j - 1] - tau
                if v > best:
                    best, arg = v, 5
            dp[i, j], back[i, j] = best, arg

    links: list[Link] = []
    gaps_a, gaps_b = [], []
    i, j = n, m
    while i > 0 or j > 0:
        step = back[i, j]
        if step == 1:
            gaps_a.append(i - 1)
            i -= 1
        elif step == 2:
            gaps_b.append(j - 1)
            j -= 1
        elif step == 3:
            links.append(Link([i - 1], [j - 1], float(S11[i - 1, j - 1])))
            i, j = i - 1, j - 1
        elif step == 4:
            links.append(Link([i - 1], [j - 2, j - 1], float(S12[i - 1, j - 2])))
            i, j = i - 1, j - 2
        elif step == 5:
            links.append(Link([i - 2, i - 1], [j - 1], float(S21[i - 2, j - 1])))
            i, j = i - 2, j - 1
        else:
            raise RuntimeError("backtrack broke")
    links.reverse()
    gaps_a.reverse()
    gaps_b.reverse()
    return Alignment(links, gaps_a, gaps_b)
