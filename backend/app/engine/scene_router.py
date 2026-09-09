"""C14 · 六大场景模板包：场景识别 + 模板装配 + 预置数据。

场景编码：mgmt_ops / store_diag / product_analysis / cost_supply / customer_marketing / auto_report
- detect_scene：关键词规则识别问题所属场景（确定性，不依赖 LLM）
- get_scene_context：取场景指标包 + 示例 + 提示词模板，装配进生成/解释上下文
- seed_scenes：系统预置 6 场景（workspace_id=None），首次启动幂等写入
"""
import logging

from ..database import SessionLocal
from ..models import SceneDef

logger = logging.getLogger(__name__)

_SCENE_KEYWORDS: dict[str, list[str]] = {
    "mgmt_ops": ["经营", "营收", "销售额", "毛利", "订单量", "客单价", "复购率", "达成率",
                 "业绩", "KPI", "增长", "同比", "环比", "总览"],
    "store_diag": ["门店", "店铺", "店均", "坪效", "人效", "单店", "分店", "连锁",
                   "店长", "门店排行", "门店诊断", "到店", "客流"],
    "product_analysis": ["商品", "产品", "SKU", "动销", "库存周转", "滞销", "畅销",
                         "品类", "爆款", "售罄", "补货", "上架", "退货率", "销售占比"],
    "cost_supply": ["成本", "采购", "供应链", "缺货", "周转天数", "供应商", "物流",
                    "运费", "损耗", "库存成本", "降本", "进货", "采购价"],
    "customer_marketing": ["客户", "会员", "新客", "老客", "复购", "客群", "营销",
                           "活动", "优惠券", "RFM", "流失", "拉新", "转化率", "召回"],
    "auto_report": ["日报", "周报", "月报", "自动报告", "报告", "总结", "报表",
                    "推送", "经营汇总", "自动生成"],
}


def detect_scene(question: str, db=None) -> str:
    """关键词规则识别场景；未命中返回空串。多场景命中取命中数最多者，平票取更专一（关键词表更短）场景。"""
    if not question:
        return ""
    q = question.lower()
    best, best_hits, best_size = "", 0, 999
    for code, kws in _SCENE_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw.lower() in q)
        if hits > best_hits or (hits == best_hits and hits > 0 and len(kws) < best_size):
            best, best_hits, best_size = code, hits, len(kws)
    return best if best_hits else ""


def get_scene_def(scene_code: str, workspace_id: int | None = None, db=None) -> SceneDef | None:
    """取场景定义：优先工作空间自定义，其次系统预置。"""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        custom = (db.query(SceneDef)
                  .filter(SceneDef.workspace_id == workspace_id,
                          SceneDef.scene_code == scene_code,
                          SceneDef.enabled.is_(True)).first())
        if custom:
            return custom
        return (db.query(SceneDef)
                .filter(SceneDef.workspace_id.is_(None),
                        SceneDef.scene_code == scene_code,
                        SceneDef.enabled.is_(True)).first())
    finally:
        if own:
            db.close()


def get_scene_context(scene_code: str, workspace_id: int | None = None, db=None) -> str:
    """装配场景提示词：指标包 + 示例问题-SQL + 生成模板。未命中返回空串。"""
    sc = get_scene_def(scene_code, workspace_id, db)
    if sc is None:
        return ""
    parts = [f"【场景：{sc.scene_name}】"]
    if sc.description:
        parts.append(sc.description)
    packs = sc.metric_pack_json or []
    if packs:
        metrics = "、".join(
            p.get("name") or p.get("metric") or str(p)[:40] for p in packs[:12])
        parts.append(f"建议指标：{metrics}")
    examples = sc.examples_json or []
    if examples:
        parts.append("参考示例：")
        for ex in examples[:3]:
            q = ex.get("question") or ex.get("q") or ""
            s = ex.get("sql") or ""
            if q:
                parts.append(f"- Q：{q}")
            if s:
                parts.append(f"  SQL：{s[:200]}")
    if sc.gen_prompt_template:
        parts.append(f"生成要求：{sc.gen_prompt_template[:300]}")
    return "\n".join(parts)


# ---------- 预置场景数据 ----------

def seed_scenes(db) -> int:
    """幂等预置六大场景模板（workspace_id=None，系统级）。返回写入数。"""
    presets = [
        {
            "scene_code": "mgmt_ops", "scene_name": "经营管理",
            "description": "面向管理层经营全景分析：营收、毛利、订单、客户、达成情况与趋势。",
            "metric_pack": [
                {"name": "销售额", "metric": "SUM(amount)", "dims": ["日期", "门店", "渠道"]},
                {"name": "毛利额", "metric": "SUM(amount - cost)", "dims": ["日期", "品类"]},
                {"name": "订单量", "metric": "COUNT(DISTINCT order_id)", "dims": ["日期", "渠道"]},
                {"name": "客单价", "metric": "SUM(amount)/COUNT(DISTINCT order_id)", "dims": ["日期"]},
            ],
            "gen_prompt_template": "优先给出按时间维度的汇总指标；涉及对比时给出同比/环比口径。",
            "explain_template": "围绕经营表现、环比变化、异常点与建议展开。",
            "examples": [
                {"question": "本月销售额和毛利率是多少，环比上月变化如何？",
                 "sql": "SELECT DATE_FORMAT(dt,'%Y-%m') m, SUM(amount) amt, "
                        "ROUND((SUM(amount)-SUM(cost))/SUM(amount),4) margin "
                        "FROM orders WHERE dt >= DATE_FORMAT(NOW(),'%Y-%m-01') "
                        "GROUP BY m ORDER BY m"},
            ],
        },
        {
            "scene_code": "store_diag", "scene_name": "门店诊断",
            "description": "门店维度诊断：店均产出、坪效、人效、排行与异常门店识别。",
            "metric_pack": [
                {"name": "店均销售额", "metric": "SUM(amount)/COUNT(DISTINCT store_id)", "dims": ["门店"]},
                {"name": "坪效", "metric": "SUM(amount)/SUM(area)", "dims": ["门店"]},
                {"name": "人效", "metric": "SUM(amount)/COUNT(employee_id)", "dims": ["门店"]},
            ],
            "gen_prompt_template": "门店对比时给出排行与 TOP/N；异常门店给出差异原因线索。",
            "examples": [
                {"question": "各门店销售额排行，找出业绩最差的 5 家门店",
                 "sql": "SELECT store_id, SUM(amount) amt FROM orders "
                        "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) "
                        "GROUP BY store_id ORDER BY amt ASC LIMIT 5"},
            ],
        },
        {
            "scene_code": "product_analysis", "scene_name": "商品分析",
            "description": "商品与 SKU 维度：动销、库存周转、滞销畅销、品类结构与补货建议。",
            "metric_pack": [
                {"name": "动销率", "metric": "COUNT(DISTINCT sold_sku)/COUNT(DISTINCT sku)", "dims": ["品类"]},
                {"name": "库存周转天数", "metric": "DATEDIFF(NOW(), min(in_date))", "dims": ["SKU"]},
                {"name": "品类销售占比", "metric": "SUM(amount)/SUM(SUM(amount)) OVER()", "dims": ["品类"]},
            ],
            "gen_prompt_template": "商品维度分析给出品类汇总与 SKU 明细；滞销/畅销明确阈值口径。",
            "examples": [
                {"question": "近 30 天品类销售占比和滞销 SKU 数量",
                 "sql": "SELECT c.category, SUM(o.amount) amt FROM orders o "
                        "JOIN product p ON o.sku_id = p.sku_id JOIN category c ON p.cat_id = c.id "
                        "WHERE o.dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) "
                        "GROUP BY c.category ORDER BY amt DESC"},
            ],
        },
        {
            "scene_code": "cost_supply", "scene_name": "成本与供应链",
            "description": "采购、库存与供应链成本：采购成本、缺货率、周转天数、供应商结构。",
            "metric_pack": [
                {"name": "采购成本", "metric": "SUM(purchase_amount)", "dims": ["供应商", "日期"]},
                {"name": "缺货率", "metric": "COUNT(stockout)/COUNT(sku)", "dims": ["门店"]},
                {"name": "周转天数", "metric": "AVG(DATEDIFF(sold_at, in_at))", "dims": ["品类"]},
            ],
            "gen_prompt_template": "成本分析给出成本构成与供应商占比；缺货与周转给出改进线索。",
            "examples": [
                {"question": "各供应商采购成本占比与近 90 天周转天数",
                 "sql": "SELECT s.supplier_name, SUM(p.amount) amt FROM purchase p "
                        "JOIN supplier s ON p.supplier_id = s.id "
                        "WHERE p.dt >= DATE_SUB(CURDATE(), INTERVAL 90 DAY) "
                        "GROUP BY s.supplier_name ORDER BY amt DESC"},
            ],
        },
        {
            "scene_code": "customer_marketing", "scene_name": "客户与营销",
            "description": "客户生命周期与营销效果：新老客、复购率、客单价、会员结构与活动效果。",
            "metric_pack": [
                {"name": "新客数", "metric": "COUNT(DISTINCT new_customer_id)", "dims": ["日期", "渠道"]},
                {"name": "复购率", "metric": "SUM(repeat_flag)/COUNT(DISTINCT customer_id)", "dims": ["月份"]},
                {"name": "客单价", "metric": "SUM(amount)/COUNT(DISTINCT order_id)", "dims": ["渠道"]},
            ],
            "gen_prompt_template": "客户分析给出新老客结构与复购表现；营销效果给出活动前后对比口径。",
            "examples": [
                {"question": "本月新老客户消费占比与复购率",
                 "sql": "SELECT CASE WHEN is_new=1 THEN '新客' ELSE '老客' END seg, "
                        "COUNT(DISTINCT customer_id) cnt, SUM(amount) amt FROM orders "
                        "WHERE dt >= DATE_FORMAT(NOW(),'%Y-%m-01') "
                        "GROUP BY seg"},
            ],
        },
        {
            "scene_code": "auto_report", "scene_name": "自动报告",
            "description": "周期性经营报告模板：日报/周报/月报结构与固定口径，支撑自动生成与推送。",
            "metric_pack": [
                {"name": "核心指标", "metric": "销售额/订单量/客单价", "dims": ["日", "周", "月"]},
                {"name": "对比口径", "metric": "同比/环比", "dims": []},
            ],
            "gen_prompt_template": "按模板输出结构化报告：概览→明细→异动→建议。",
            "report_template": "## {周期}经营报告\n### 概览\n- 销售额：{sales}\n"
                               "- 订单量：{orders}\n- 客单价：{aov}\n### 异动\n{anomalies}\n### 建议\n{suggestions}",
            "examples": [
                {"question": "生成昨日经营日报",
                 "sql": "SELECT DATE(dt) d, SUM(amount) amt, COUNT(DISTINCT order_id) cnt "
                        "FROM orders WHERE dt = DATE_SUB(CURDATE(), INTERVAL 1 DAY) GROUP BY d"},
            ],
        },
    ]
    n = 0
    for p in presets:
        exists = (db.query(SceneDef)
                  .filter(SceneDef.workspace_id.is_(None),
                          SceneDef.scene_code == p["scene_code"]).first())
        if exists:
            continue
        db.add(SceneDef(workspace_id=None, scene_code=p["scene_code"],
                        scene_name=p["scene_name"], description=p["description"],
                        metric_pack_json=p.get("metric_pack", []),
                        gen_prompt_template=p.get("gen_prompt_template", ""),
                        explain_template=p.get("explain_template", ""),
                        examples_json=p.get("examples", []),
                        report_template=p.get("report_template", ""),
                        enabled=True,
                        sort_order=["mgmt_ops", "store_diag", "product_analysis",
                                    "cost_supply", "customer_marketing", "auto_report"]
                        .index(p["scene_code"])))
        n += 1
    if n:
        db.commit()
        logger.info("[scene] 预置 %d 个场景模板", n)
    return n
