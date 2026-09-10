"""公共图表推荐器（C14 扩展）。

规则引擎 + 注册式：按 intent + 数据特征推荐图表类型。
新增图表类型只需实现 RecommendRule 接口并注册。
"""
from __future__ import annotations

from typing import Any


# ---------- 推荐规则接口 ----------
class RecommendRule:
    """图表推荐规则接口。"""

    def match(self, intent: str, metrics: list, dimensions: list,
              row_count: int, col_count: int) -> bool:
        raise NotImplementedError

    def recommend(self) -> str:
        raise NotImplementedError


# ---------- 内置规则 ----------
class RatioPieRule(RecommendRule):
    """占比/构成（ratio）且 2–8 行 → 饼图。

    优先级高于 RankingRule：用户问「前三的占比记录」同时含排名与占比语义，
    若按 ranking 出排行榜，占比数值既不上图也不是真正的构成展示；
    ratio 语义下饼图更能表达占比构成。
    行数 1 时不匹配（单值答案交给 KPI）。
    """

    def match(self, intent, metrics, dimensions, row_count, col_count,
              ratio: bool = False):
        return ratio and 1 < row_count <= 8 and len(metrics) >= 1 and len(dimensions) >= 1

    def recommend(self):
        return "pie"


class RankingRule(RecommendRule):
    def match(self, intent, metrics, dimensions, row_count, col_count):
        return intent == "ranking" or "排行" in str(metrics) or "排名" in str(metrics)

    def recommend(self):
        return "rank"


class TrendRule(RecommendRule):
    def match(self, intent, metrics, dimensions, row_count, col_count):
        if intent == "trend":
            return True
        # 维度含时间字段
        time_keywords = ("时间", "日期", "月", "日", "年", "周", "季度", "date", "time", "month")
        for d in dimensions:
            name = getattr(d, "name", str(d))
            if any(k in name.lower() for k in time_keywords):
                return True
        return False

    def recommend(self):
        return "line"


class StatisticSingleRule(RecommendRule):
    """统计意图 + 单行单列 → KPI 卡片。"""

    def match(self, intent, metrics, dimensions, row_count, col_count):
        return intent == "statistic" and row_count <= 1 and len(metrics) <= 2

    def recommend(self):
        return "kpi"


class ValueSingleRule(RecommendRule):
    """数值意图 + 单行 → KPI。"""

    def match(self, intent, metrics, dimensions, row_count, col_count):
        return intent == "value" and row_count <= 1 and len(dimensions) == 0

    def recommend(self):
        return "kpi"


class RadarRule(RecommendRule):
    """3+ 指标对比 → 雷达图。"""

    def match(self, intent, metrics, dimensions, row_count, col_count):
        return intent == "compare" and len(metrics) >= 3 and row_count <= 5

    def recommend(self):
        return "radar"


class ComboRule(RecommendRule):
    """2 指标对比 → 双Y轴柱线组合。"""

    def match(self, intent, metrics, dimensions, row_count, col_count):
        return intent == "compare" and len(metrics) == 2 and row_count > 1

    def recommend(self):
        return "combo"


class StackBarRule(RecommendRule):
    """2+ 指标 + 多维度 → 堆叠柱状图。"""

    def match(self, intent, metrics, dimensions, row_count, col_count):
        return len(metrics) >= 2 and len(dimensions) >= 1 and row_count > 1

    def recommend(self):
        return "stack_bar"


class PieRule(RecommendRule):
    """≤8 类 + 单指标 → 饼图。"""

    def match(self, intent, metrics, dimensions, row_count, col_count):
        return len(metrics) == 1 and 1 < row_count <= 8 and len(dimensions) == 1

    def recommend(self):
        return "pie"


class GroupBarRule(RecommendRule):
    """多指标 + 分类维度 → 分组柱状图。"""

    def match(self, intent, metrics, dimensions, row_count, col_count):
        return len(metrics) >= 2 and len(dimensions) >= 1

    def recommend(self):
        return "group_bar"


# ---------- 公共推荐器 ----------
class ChartRecommender:
    """图表推荐器：规则引擎 + 注册式。

    用法：
        recommender = ChartRecommender()
        chart_type = recommender.recommend(intent, metrics, dimensions, row_count, col_count)
        layout = recommender.recommend_layout(n, hints)
    """

    _rules: list[tuple[int, RecommendRule]] = []

    def __init__(self):
        if not ChartRecommender._rules:
            self._register_builtin()

    @classmethod
    def _register_builtin(cls):
        """注册内置规则（按优先级降序）。"""
        builtin = [
            (105, RatioPieRule()),
            (100, RankingRule()),
            (95, TrendRule()),
            (90, StatisticSingleRule()),
            (88, ValueSingleRule()),
            (80, RadarRule()),
            (75, ComboRule()),
            (70, StackBarRule()),
            (60, PieRule()),
            (50, GroupBarRule()),
        ]
        cls._rules = builtin

    @classmethod
    def register(cls, rule: RecommendRule, priority: int = 0) -> None:
        """注册新推荐规则。"""
        cls._rules.append((priority, rule))
        cls._rules.sort(key=lambda x: x[0], reverse=True)

    def recommend(self, intent: str, metrics: list | None = None,
                  dimensions: list | None = None,
                  row_count: int = 0, col_count: int = 0,
                  ratio: bool = False) -> str:
        """推荐图表类型：遍历规则链，首个 match 的规则胜出。

        ratio: 占比/构成语义提示（子查询问题含 占比/比例/构成 或 stat=ratio）。
        """
        metrics = metrics or []
        dimensions = dimensions or []
        for _, rule in self._rules:
            try:
                if rule.match(intent, metrics, dimensions, row_count, col_count,
                              ratio=ratio):
                    return rule.recommend()
            except TypeError:
                # 旧规则接口无 ratio 参数：退化为不带 ratio 的匹配
                try:
                    if rule.match(intent, metrics, dimensions, row_count, col_count):
                        return rule.recommend()
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue
        return "bar"  # 默认柱状图

    def recommend_layout(self, n: int, hints: list[str] | None = None) -> list[dict]:
        """推荐布局：返回 [{index, span}]，12 栅格。

        规则：
        - kpi → span=3（一行 4 个）
        - rank → span=4（一行 3 个）
        - combo/radar → span=8（大卡片，一行 1-2 个）
        - 其余 → 12/n（平均分配）
        """
        hints = hints or []
        layout = []
        for i in range(n):
            hint = hints[i] if i < len(hints) else ""
            if hint == "kpi":
                span = 3
            elif hint == "rank":
                span = 4
            elif hint in ("combo", "radar", "sankey", "funnel"):
                span = 8
            elif n == 1:
                span = 12
            elif n == 2:
                span = 6
            elif n == 3:
                span = 4
            elif n == 4:
                span = 6  # 2x2
            else:
                span = max(3, 12 // n)
            layout.append({"index": i, "span": span})
        return layout
