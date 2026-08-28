from .base_ecommerce_tool import TOOL_REGISTRY, register_tool, EcommerceBaseTool

from .open_store import OpenStore
from .close_store import CloseStore
from .check_store_status import CheckStoreStatus
from .list_products import ListProducts
from .stock_store import PublishToStore
from .set_prices import SetPrices
from .return_to_warehouse import ReturnToWarehouse
from .join_promotion import JoinPromotion
from .check_balance import CheckBalance
from .check_warehouse import CheckWarehouse
from .withdraw import Withdraw
from .wait_for_next_day import WaitForNextDay
from .chatbox import Chatbox
from .operate_memory import OperateMemory
from .market_search import MarketSearch
from .supplier_search import SupplierSearch
from .ship_orders import ShipOrders
from .trace_return_sources import TraceReturnSources

ALL_ECOMMERCE_TOOLS = list(TOOL_REGISTRY.values())

ECOMMERCE_TOOL_SCHEMAS = [tool.get_info()["function"] for tool in ALL_ECOMMERCE_TOOLS]

ecommerce_tool_map = {
    tool.get_info()["function"]["name"]: tool for tool in ALL_ECOMMERCE_TOOLS
}
