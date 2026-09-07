"""L-0 公共化地基层：业务词表统一注册中心。

将散落于 intent._INTENT_SIGNALS / _STATUS_WORDS / _COUNT_KW、
preprocess._TYPO_MAP / _FILLER / _DEIXIS、
nl2sql._FILE_KW / _SIZE_KW、rag._GENERIC_QWORDS、
chart._ratio_hints / mapping._BUCKET_SUFFIXES 等 10+ 处词表
统一收拢为可配置的注册中心。

正常路径（L-A）不使用词表；词表仅在 L-C 兜底路径使用。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 全局词表注册中心
_LEXICON: dict[str, tuple[str, ...]] = {}

# 是否已初始化内置词表
_INITIALIZED = False


def register(category: str, words: list[str] | tuple[str, ...]) -> None:
    """注册一个词表分类。如 register("status", ("已完成","未完成",...))。"""
    if not category or not words:
        return
    clean = tuple(w for w in words if w and str(w).strip())
    if clean:
        _LEXICON[category] = clean


def match(category: str, text: str) -> bool:
    """判断 text 是否命中指定分类的任意词。"""
    if not category or not text:
        return False
    words = _LEXICON.get(category)
    if not words:
        return False
    return any(w in text for w in words)


def get(category: str) -> tuple[str, ...]:
    """获取指定分类的完整词表。供 fallback 路径遍历使用。"""
    return _LEXICON.get(category, ())


def categories() -> dict[str, tuple[str, ...]]:
    """获取全量词表（调试/展示用）。"""
    return dict(_LEXICON)


def _init_builtin_lexicon() -> None:
    """注册内置业务词表（迁自各模块散落的词表定义）。"""
    global _INITIALIZED
    if _INITIALIZED:
        return

    # 业务实体词（迁自 intent._business_kw / rewrite_question 内部词表）
    register("business_entity", (
        "文件", "审批", "附件", "订单", "流程", "项目", "用例", "任务",
        "记录", "数据", "报告", "薪资", "考勤", "部门", "员工", "接口", "用户",
    ))

    # 状态词（迁自 intent._STATUS_WORDS）
    register("status", (
        "已完成", "未完成", "已付款", "未付款", "已支付", "未支付", "待审核",
        "已审核", "已发货", "待发货", "线下", "线上", "有效", "无效",
        "启用", "停用", "成功", "失败", "正常", "异常",
    ))

    # 语气词/前缀词（迁自 preprocess._FILLER_PREFIXES）
    register("filler_prefix", (
        "帮我查一下", "帮我查", "帮我看看", "帮我", "麻烦查一下", "麻烦查询", "麻烦",
        "我想查一下", "我想查询", "我想看看", "我想", "请问一下", "请问", "能不能帮我",
        "可以帮我", "能帮我", "我想了解一下", "了解一下", "查一下", "查询一下",
        "看下", "看一下", "看看", "能不能", "可以吗", "好吗", "呗",
    ))

    # 指代消解信号（迁自 preprocess._DEIXIS_SIGNALS）
    register("deixis_signal", (
        "环比", "同比", "呢", "接着", "继续", "再按", "按", "只看", "看看", "那",
        "分别", "分月", "分日", "分周", "分季度", "拆分", "分组", "对比", "比较",
        "涨", "跌", "升", "降", "增", "减", "排", "占比", "趋势", "多少", "几个",
        "前", "后",
    ))

    # 错别字映射（迁自 preprocess._TYPO_MAP，保留 dict 形式供 correct_biz_words 使用）
    register("typo_map", (
        "定单", "定单量", "销受", "xiaoshoue", "shouru", "lirun", "chengben",
        "yonghu", "huoli", "furong", "tuikuan", "zhifu", "shangpin", "qudao",
        "diqu", "chengshi",
    ))

    # 通用疑问词（迁自 rag._GENERIC_QWORDS）
    register("generic_question", (
        "哪些", "什么", "如何", "怎么", "怎样", "为啥", "为什么", "是否", "多少",
    ))

    # 占比词（迁自 chart._ratio_hints + intent 占比信号）
    register("ratio", (
        "占比", "比例", "百分比", "构成", "份额", "rate", "ratio",
        "percent", "pct", "proportion", "share",
    ))

    # 分箱/区间词（迁自 mapping._BUCKET_SUFFIXES + intent._bucket_signal）
    register("bucket", ("范围", "区间", "分布"))

    # 文件/附件信号（迁自 nl2sql._FILE_KW / _SIZE_KW）
    register("file_signal", (
        "file", "attach", "upload", "文件", "附件", "上传", "archive", "pdf",
        "文件大小", "字节", "大小", "size",
    ))

    # 计数语境词（迁自 intent._CNT_CTX_KW）
    register("count_context", (
        "次数", "多少个", "多少条", "几条", "数量", "调用量", "访问量",
        "请求数", "请求次数", "被调用", "执行次数", "命中次数", "总数", "总共有",
    ))

    # 数量问词（迁自 intent._NUM_ASK_WORDS）
    register("num_ask", (
        "多少", "几个", "几条", "分别", "各个", "各", "总共", "一共", "总计", "总数", "总共有",
    ))

    # 明细指标词（迁自 intent._DETAIL_METRIC_KW）
    register("detail_metric", (
        "金额", "价格", "费用", "成本", "利润", "数量", "次数", "人数",
        "总量", "总数", "合计", "均值", "平均", "占比", "比例",
        "时长", "大小", "重量", "收入", "支出", "余额",
    ))

    # 明细维度词（迁自 intent._DETAIL_DIM_KW）
    register("detail_dim", (
        "类型", "状态", "渠道", "地区", "城市", "省份", "部门", "类别",
        "平台", "来源", "名称", "方式", "层级", "分组", "级别", "行业",
    ))

    # 分组统计信号（迁自 intent._group_kw）
    register("group_signal", (
        "每个", "各个", "分别", "各项目", "各渠道", "各地区", "各部门",
        "各类型", "各状态",
    ))

    _INITIALIZED = True


def load_from_db(workspace_id: int) -> None:
    """从数据库 biz_lexicon 表加载业务词表（支持业务方自助维护，不改代码）。

    无 DB 记录时使用内置默认词表（_init_builtin_lexicon）。
    """
    _init_builtin_lexicon()
    try:
        from ..database import SessionLocal
        from ..models import IntentDict
        db = SessionLocal()
        try:
            rows = (db.query(IntentDict)
                    .filter(IntentDict.workspace_id == workspace_id,
                            IntentDict.enabled.is_(True))
                    .all())
            for r in rows:
                if r.dict_type and r.term:
                    cat = r.dict_type
                    existing = list(_LEXICON.get(cat, ()))
                    if r.term not in existing:
                        existing.append(r.term)
                        _LEXICON[cat] = tuple(existing)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("biz_lexicon DB 加载失败（使用内置词表）: %s", exc)


# 模块加载时自动初始化内置词表
_init_builtin_lexicon()
