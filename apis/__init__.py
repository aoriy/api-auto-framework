"""apis：页面对象层（API Object）。一个页面 = 一个类，一个接口 = 一个方法。

普通业务域：xxx_api.py / XxxApi
流程页面：  xxx_page.py / XxxPageApi   ← 与真实系统的「页面」一一对应，便于对照替换
"""
from apis.base_api import BaseApi

# ---- 流程页面模板（8 个页面，对应真实系统的 8 个页面） ----
from apis.apply_page import ApplyPageApi
from apis.completed_page import CompletedPageApi
from apis.expert_review_page import ExpertReviewPageApi
from apis.fae_lead_review_page import FaeLeadReviewPageApi
from apis.fae_summary_page import FaeSummaryPageApi
from apis.investigating_page import InvestigatingPageApi
from apis.investigation_summary_page import InvestigationSummaryPageApi
from apis.ipq_review_page import IpqReviewPageApi

# ---- 既有的业务域页面对象 ----
from apis.flow_api import FlowApi
from apis.order_api import OrderApi
from apis.partner_api import PartnerApi
from apis.product_api import ProductApi
from apis.shop_api import ShopApi
from apis.user_api import UserApi

__all__ = [
    "BaseApi",
    "UserApi", "ProductApi", "OrderApi", "ShopApi", "PartnerApi", "FlowApi",
    # 流程页面模板
    "ApplyPageApi",
    "ExpertReviewPageApi",
    "FaeSummaryPageApi",
    "IpqReviewPageApi",
    "FaeLeadReviewPageApi",
    "InvestigatingPageApi",
    "InvestigationSummaryPageApi",
    "CompletedPageApi",
]
