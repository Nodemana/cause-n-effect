"""XBRL concepts this scout will read, and the proxies it will not.

Backlog is taken from remaining performance obligation and from tags named
in the customer universe. Deferred revenue and contract liabilities are not
backlog, and figures are never inferred from them.
"""

# ASC 606 remaining performance obligations. The usual structured backlog tag.
ACTIVE_BACKLOG_TAGS = ("RevenueRemainingPerformanceObligation",)

# Concepts a filer may have stopped using. Facts are kept for the agent and
# are not treated as the current backlog number.
STALE_BACKLOG_TAGS = ("BacklogOfOrders", "Backlog")

# Order-intake concepts some suppliers tag. Absent from most filers; the
# quarterly-release parser covers the untagged case.
ACTIVE_INTAKE_TAGS = ("OrdersReceived", "OrderIntake")

# Explicitly ignored. A test locks this list.
PROXY_TAGS = (
    "DeferredRevenue",
    "DeferredRevenueCurrent",
    "DeferredRevenueNoncurrent",
    "ContractWithCustomerLiability",
    "ContractWithCustomerLiabilityCurrent",
    "ContractWithCustomerLiabilityNoncurrent",
)

CUSTOMER_SECTORS = ("defense", "industrial", "semiconductor_equipment")
SUPPLIER_EQUIPMENT = ("turbines", "transformers", "switchgear")
