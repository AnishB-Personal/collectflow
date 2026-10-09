from fastapi import APIRouter, Depends

from app.database import query_all, query_one
from app.routers.auth import get_current_user
from app.utils.financial import get_current_financial_year

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


@router.get("/summary")
def get_dashboard_summary(user: dict = Depends(get_current_user)):
    current_fy = get_current_financial_year()

    # 1. High-level KPIs (Cancelled invoices have 0 gross and 0 balance, but preserve any historical settlements)
    kpi_query = """
    SELECT
        COUNT(DISTINCT CASE WHEN computed_status != 'CANCELLED' THEN id END) as total_invoices,
        COALESCE(SUM(CASE WHEN computed_status != 'CANCELLED' THEN total_amount ELSE 0 END), 0) as total_invoiced,
        COALESCE(SUM(total_cash_paid), 0) as total_cash_collected,
        COALESCE(SUM(total_tds_deducted), 0) as total_tds_recognized,
        COALESCE(SUM(total_paid), 0) as total_settled,
        COALESCE(SUM(total_paid), 0) as total_collected,
        COALESCE(SUM(outstanding_balance), 0) as total_outstanding,
        COALESCE(SUM(CASE WHEN fy_code = ? THEN outstanding_balance ELSE 0 END), 0) as current_fy_outstanding,
        COALESCE(SUM(CASE WHEN fy_code = ? AND computed_status != 'CANCELLED' THEN total_amount ELSE 0 END), 0) as current_fy_invoiced,
        COALESCE(SUM(CASE WHEN fy_code = ? THEN total_paid ELSE 0 END), 0) as current_fy_collected
    FROM v_invoice_summary;
    """
    kpis = query_one(kpi_query, (current_fy, current_fy, current_fy)) or {}
    total_inv = kpis.get("total_invoiced", 0)
    total_col = kpis.get("total_settled", 0)
    kpis["current_fy_code"] = current_fy
    kpis["overall_collection_rate"] = round((total_col / total_inv * 100), 1) if total_inv > 0 else 0.0

    # 2. Year-over-Year Summary
    yoy_query = """
    SELECT
        fy_code,
        COUNT(CASE WHEN computed_status != 'CANCELLED' THEN id END) as total_orders,
        COALESCE(SUM(CASE WHEN computed_status != 'CANCELLED' THEN exclusive_amount ELSE 0 END), 0) as exclusive_value,
        COALESCE(SUM(CASE WHEN computed_status != 'CANCELLED' THEN total_amount ELSE 0 END), 0) as invoiced_gross,
        COALESCE(SUM(total_cash_paid), 0) as cash_collected,
        COALESCE(SUM(total_tds_deducted), 0) as tds_deducted,
        COALESCE(SUM(total_paid), 0) as collections,
        COALESCE(SUM(outstanding_balance), 0) as balance_outstanding,
        CASE
            WHEN SUM(CASE WHEN computed_status != 'CANCELLED' THEN total_amount ELSE 0 END) > 0
            THEN ROUND((SUM(total_paid) / SUM(CASE WHEN computed_status != 'CANCELLED' THEN total_amount ELSE 0 END)) * 100, 1)
            ELSE 0.0
        END as collection_rate
    FROM v_invoice_summary
    GROUP BY fy_code
    ORDER BY fy_code ASC;
    """
    yoy = query_all(yoy_query)

    # 3. Aging Distribution
    aging_query = """
    SELECT
        CASE
            WHEN days_overdue <= 0 THEN 'Current / Not Due'
            WHEN days_overdue BETWEEN 1 AND 30 THEN '1 - 30 Days'
            WHEN days_overdue BETWEEN 31 AND 60 THEN '31 - 60 Days'
            WHEN days_overdue BETWEEN 61 AND 90 THEN '61 - 90 Days'
            WHEN days_overdue BETWEEN 91 AND 180 THEN '91 - 180 Days'
            ELSE '180+ Days'
        END as bucket,
        COUNT(id) as invoice_count,
        COALESCE(SUM(outstanding_balance), 0) as outstanding_amount
    FROM v_invoice_summary
    WHERE outstanding_balance > 0
    GROUP BY bucket
    ORDER BY
        CASE bucket
            WHEN 'Current / Not Due' THEN 1
            WHEN '1 - 30 Days' THEN 2
            WHEN '31 - 60 Days' THEN 3
            WHEN '61 - 90 Days' THEN 4
            WHEN '91 - 180 Days' THEN 5
            ELSE 6
        END;
    """
    aging = query_all(aging_query)

    # 4. Executive Performance (excluding cancelled sales)
    exec_query = """
    SELECT
        u.id as user_id,
        u.id,
        COALESCE(u.full_name, u.username) as name,
        COUNT(CASE WHEN vis.fy_code = ? AND vis.computed_status != 'CANCELLED' THEN vis.id END) as fy26_27_orders,
        COALESCE(SUM(CASE WHEN vis.fy_code = ? AND vis.computed_status != 'CANCELLED' THEN vis.exclusive_amount ELSE 0 END), 0) as fy26_27_sales,
        COALESCE(SUM(CASE WHEN vis.fy_code = ? THEN vis.outstanding_balance ELSE 0 END), 0) as fy26_27_outstanding,
        COALESCE(SUM(CASE WHEN vis.computed_status != 'CANCELLED' THEN vis.exclusive_amount ELSE 0 END), 0) as all_time_sales,
        COALESCE(st.yearly_target, 0) as yearly_target,
        COALESCE(st.half_yearly_target, 0) as half_yearly_target,
        COALESCE(st.quarterly_target, 0) as quarterly_target,
        COALESCE(st.monthly_target, 0) as monthly_target,
        CASE
            WHEN COALESCE(st.yearly_target, 0) > 0
            THEN ROUND((SUM(CASE WHEN vis.fy_code = ? AND vis.computed_status != 'CANCELLED' THEN vis.exclusive_amount ELSE 0 END) / st.yearly_target) * 100, 1)
            ELSE 0.0
        END as target_achievement_pct
    FROM users u
    LEFT JOIN v_invoice_summary vis ON (vis.executive_id = u.id)
    LEFT JOIN sales_targets st ON u.id = st.user_id AND st.fy_code = ?
    WHERE u.role = 'sales'
    GROUP BY u.id
    ORDER BY fy26_27_sales DESC;
    """
    executives = query_all(exec_query, (current_fy, current_fy, current_fy, current_fy, current_fy))

    return {"kpis": kpis, "yoy": yoy, "aging": aging, "executives": executives}
