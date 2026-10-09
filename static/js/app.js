        let currentInvoicePage = 1;
        let invoiceDebounceTimer, customerDebounceTimer;
        let yoyChartInst = null, agingChartInst = null;
        let currentUser = null;
        let cachedInvoices = [];
        let cachedCustomers = [];
        let cachedUsers = [];
        let cachedFYs = [];

        function fmtCurr(val) {
            return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(val || 0);
        }

        function escapeHtml(str) {
            if (str == null || str === undefined) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function getAuthHeaders(extraHeaders = {}) {
            const token = localStorage.getItem('cf_auth_token');
            const headers = { ...extraHeaders };
            if (token) {
                headers['Authorization'] = `Bearer ${token}`;
            }
            return headers;
        }

        async function authFetch(url, options = {}) {
            options.headers = getAuthHeaders(options.headers || {});
            const res = await fetch(url, options);
            if (res.status === 401) {
                localStorage.removeItem('cf_auth_token');
                showLogin();
            }
            return res;
        }

        async function downloadExcelExport() {
            const fyElem = document.getElementById('filterFY');
            const fy = fyElem ? fyElem.value : 'ALL';
            try {
                const res = await authFetch('/api/export/token', { method: 'POST' });
                if (!res.ok) {
                    alert('Export authorization failed. Please log in again.');
                    return;
                }
                const data = await res.json();
                window.location.href = `/api/export/excel?fy=${encodeURIComponent(fy)}&ticket=${encodeURIComponent(data.ticket)}`;
            } catch (err) {
                alert('Export failed: ' + err.message);
            }
        }

        function switchTab(tabId) {
            ['dashboard', 'invoices', 'customers', 'settings'].forEach(t => {
                document.getElementById(`tab-${t}`).classList.add('hidden');
                document.getElementById(`tabBtn-${t}`).className = 'py-3 text-slate-400 hover:text-slate-200 focus:outline-none';
            });
            document.getElementById(`tab-${tabId}`).classList.remove('hidden');
            document.getElementById(`tabBtn-${tabId}`).className = 'py-3 text-blue-400 border-b-2 border-blue-400 font-semibold focus:outline-none';
            if (tabId === 'invoices') fetchInvoices(1);
            if (tabId === 'customers') fetchCustomers();
            if (tabId === 'settings') fetchSettingsMasters();
        }

        function openModal(id) { document.getElementById(id).classList.remove('hidden'); if(id === 'invoiceModal' && document.querySelectorAll('#lineItemsTableBody tr').length === 0) addLineItemRow(); }
        function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

        let cachedTaxRates = [];
        let cachedTDSRates = [];

        async function initApp() {
            // Load Metadata (FYs, Executives, Customers, Tax Rates, TDS Rates)
            const metaRes = await authFetch('/api/meta');
            if (!metaRes.ok) return;
            const meta = await metaRes.json();

            cachedTaxRates = meta.tax_rates || [];
            cachedTDSRates = meta.tds_rates || [];

            const fySelect = document.getElementById('filterFY');
            const formFY = document.getElementById('formFY');
            fySelect.innerHTML = '<option value="ALL">All Financial Years</option>';
            formFY.innerHTML = '';
            meta.financial_years.forEach(fy => {
                fySelect.innerHTML += `<option value="${escapeHtml(fy)}">${escapeHtml(fy)}</option>`;
                formFY.innerHTML += `<option value="${escapeHtml(fy)}">${escapeHtml(fy)}</option>`;
            });

            const formCust = document.getElementById('formCustomer');
            formCust.innerHTML = '';
            meta.customers.forEach(c => {
                formCust.innerHTML += `<option value="${c.id}">${escapeHtml(c.billing_name)}</option>`;
            });

            const formExec = document.getElementById('formExecutive');
            const filterExec = document.getElementById('filterExecutive');
            formExec.innerHTML = '';
            if (filterExec) filterExec.innerHTML = '<option value="ALL">All Sales Executives</option>';
            meta.executives.forEach(e => {
                formExec.innerHTML += `<option value="${e.id}">${escapeHtml(e.name)}</option>`;
                if (filterExec) filterExec.innerHTML += `<option value="${e.id}">${escapeHtml(e.name)}</option>`;
            });

            // Populate TDS Options in Payment Modal
            const payTDSSelect = document.getElementById('payTDSSection');
            if (payTDSSelect) {
                payTDSSelect.innerHTML = '<option value="" data-rate="0">-- None / No TDS --</option>';
                cachedTDSRates.forEach(t => {
                    const pct = (t.rate * 100).toFixed(t.rate * 100 % 1 === 0 ? 0 : 1);
                    payTDSSelect.innerHTML += `<option value="${escapeHtml(t.section_code)}" data-rate="${t.rate}">${escapeHtml(t.section_code)} - ${pct}% (${escapeHtml(t.description || '')})</option>`;
                });
            }

            // Set default date to today
            document.getElementById('payDate').value = new Date().toISOString().split('T')[0];

            loadDashboard();
        }

        async function loadDashboard() {
            const res = await authFetch('/api/dashboard/summary');
            if (!res.ok) return;
            const data = await res.json();

            // KPIs
            document.getElementById('kpi-invoiced').textContent = fmtCurr(data.kpis.total_invoiced);
            document.getElementById('kpi-collected').textContent = fmtCurr(data.kpis.total_collected);
            document.getElementById('kpi-rate').textContent = `${data.kpis.overall_collection_rate}% collection rate`;
            document.getElementById('kpi-outstanding').textContent = fmtCurr(data.kpis.total_outstanding);
            document.getElementById('kpi-current-fy').textContent = fmtCurr(data.kpis.current_fy_outstanding);

            // Render YoY Chart
            const ctxY = document.getElementById('yoyChart').getContext('2d');
            if (yoyChartInst) yoyChartInst.destroy();
            yoyChartInst = new Chart(ctxY, {
                type: 'bar',
                data: {
                    labels: data.yoy.map(d => d.fy_code),
                    datasets: [
                        { label: 'Invoiced Gross', data: data.yoy.map(d => d.invoiced_gross), backgroundColor: '#3b82f6' },
                        { label: 'Collections', data: data.yoy.map(d => d.collections), backgroundColor: '#10b981' }
                    ]
                },
                options: { responsive: true, maintainAspectRatio: false }
            });

            // Render Aging Chart
            const ctxA = document.getElementById('agingChart').getContext('2d');
            if (agingChartInst) agingChartInst.destroy();
            agingChartInst = new Chart(ctxA, {
                type: 'doughnut',
                data: {
                    labels: data.aging.map(a => a.bucket),
                    datasets: [{
                        data: data.aging.map(a => a.outstanding_amount),
                        backgroundColor: ['#10b981', '#3b82f6', '#f59e0b', '#f97316', '#ef4444', '#b91c1c']
                    }]
                },
                options: { responsive: true, maintainAspectRatio: false }
            });

            // Render Executive Table
            const tb = document.getElementById('executiveTableBody');
            tb.innerHTML = data.executives.map(e => `
                <tr class="hover:bg-slate-50 transition">
                    <td class="px-6 py-3.5 font-medium text-slate-900">${escapeHtml(e.name)}</td>
                    <td class="px-6 py-3.5 text-right">${e.fy26_27_orders}</td>
                    <td class="px-6 py-3.5 text-right font-semibold">${fmtCurr(e.fy26_27_sales)}</td>
                    <td class="px-6 py-3.5 text-right text-slate-500">${fmtCurr(e.yearly_target)}</td>
                    <td class="px-6 py-3.5 text-right font-bold ${e.target_achievement_pct >= 80 ? 'text-emerald-600' : 'text-slate-700'}">${e.target_achievement_pct}%</td>
                    <td class="px-6 py-3.5 text-right text-amber-600">${fmtCurr(e.fy26_27_outstanding)}</td>
                </tr>
            `).join('');
        }

        async function fetchInvoices(page = 1) {
            currentInvoicePage = page;
            const search = document.getElementById('invoiceSearch').value;
            const fy = document.getElementById('filterFY').value;
            const status = document.getElementById('filterStatus').value;
            const execFilter = document.getElementById('filterExecutive');
            const execId = execFilter && execFilter.value !== 'ALL' ? execFilter.value : '';

            let url = `/api/invoices?page=${page}&limit=25&search=${encodeURIComponent(search)}&fy=${encodeURIComponent(fy)}&status=${encodeURIComponent(status)}`;
            if (execId) {
                url += `&executive_id=${encodeURIComponent(execId)}`;
            }

            const res = await authFetch(url);
            if (!res.ok) return;
            const resData = await res.json();

            document.getElementById('invoiceCountText').textContent = `Showing ${(page - 1)*25 + 1} - ${Math.min(page*25, resData.total)} of ${resData.total.toLocaleString()} records`;
            document.getElementById('pageNumberText').textContent = `Page ${page} of ${resData.total_pages || 1}`;
            document.getElementById('btnPrevPage').disabled = page <= 1;
            document.getElementById('btnNextPage').disabled = page >= resData.total_pages;

            const tb = document.getElementById('invoiceTableBody');
            cachedInvoices = resData.data || [];
            if (!cachedInvoices.length) {
                tb.innerHTML = `<tr><td colspan="8" class="px-6 py-12 text-center text-slate-400">No matching orders or invoices found</td></tr>`;
                return;
            }

            tb.innerHTML = cachedInvoices.map(inv => {
                let badgeClass = 'bg-slate-100 text-slate-700 border-slate-200';
                if (inv.computed_status === 'PAID') badgeClass = 'bg-emerald-50 text-emerald-700 border-emerald-200';
                else if (inv.computed_status === 'PARTIAL') badgeClass = 'bg-blue-50 text-blue-700 border-blue-200';
                else if (inv.computed_status === 'OVERDUE') badgeClass = 'bg-rose-50 text-rose-700 border-rose-200 font-semibold';
                else if (inv.computed_status === 'PENDING') badgeClass = 'bg-amber-50 text-amber-700 border-amber-200';
                else if (inv.computed_status === 'UNBILLED') badgeClass = 'bg-purple-50 text-purple-700 border-purple-200 font-bold';

                const isUnbilled = inv.computed_status === 'UNBILLED';
                const paidPct = inv.total_amount > 0 ? Math.min(100, Math.round((inv.total_paid / inv.total_amount) * 100)) : (inv.outstanding_balance <= 0 ? 100 : 0);

                let overdueText = '';
                if (inv.computed_status === 'OVERDUE' && inv.days_overdue > 0) {
                    overdueText = `<span class="text-[11px] text-rose-600 font-medium block">${inv.days_overdue}d overdue</span>`;
                }

                const canManage = currentUser && (currentUser.role === 'admin' || currentUser.role === 'finance');

                return `
                    <tr class="hover:bg-slate-50/80 transition group">
                        <!-- 1. Customer (Name only, click to view details) -->
                        <td class="px-5 py-3.5 align-middle">
                            <button type="button" onclick="openViewOrderModal(${inv.id})" class="font-semibold text-slate-900 hover:text-blue-600 text-left transition" title="Click to view full details">
                                ${escapeHtml(inv.customer_name || 'Unknown Customer')}
                            </button>
                        </td>

                        <!-- 2. PO No -->
                        <td class="px-5 py-3.5 align-middle font-medium text-slate-800">
                            ${escapeHtml(inv.po_number || '-')}
                        </td>

                        <!-- 3. Invoice No -->
                        <td class="px-5 py-3.5 align-middle">
                            ${inv.invoice_number ? `
                                <span class="font-mono text-xs font-semibold text-blue-700 bg-blue-50 px-2 py-0.5 rounded border border-blue-100">
                                    ${escapeHtml(inv.invoice_number)}
                                </span>
                            ` : (isUnbilled ? `
                                <span class="text-xs text-purple-600 font-semibold italic">Unbilled PO</span>
                            ` : `<span class="text-xs text-slate-400">-</span>`)}
                        </td>

                        <!-- 4. Sales Representative (Full name, no short forms) -->
                        <td class="px-5 py-3.5 align-middle text-slate-700 font-medium">
                            ${escapeHtml(inv.executive_name || 'Unassigned')}
                        </td>

                        <!-- 5. Invoiced Amount -->
                        <td class="px-5 py-3.5 align-middle text-right font-semibold text-slate-900">
                            ${fmtCurr(inv.total_amount)}
                        </td>

                        <!-- 6. Balance & Settlement -->
                        <td class="px-5 py-3.5 align-middle text-right">
                            <div class="font-bold ${inv.outstanding_balance > 0 ? 'text-amber-600' : 'text-slate-400'}">
                                ${fmtCurr(inv.outstanding_balance)}
                            </div>
                            <div class="flex items-center justify-end space-x-1.5 mt-0.5">
                                <div class="w-14 bg-slate-100 rounded-full h-1.5 overflow-hidden">
                                    <div class="bg-emerald-500 h-1.5 rounded-full" style="width: ${paidPct}%"></div>
                                </div>
                                <span class="text-[10px] text-emerald-600 font-medium">${fmtCurr(inv.total_paid)}</span>
                            </div>
                        </td>

                        <!-- 7. Due Date -->
                        <td class="px-5 py-3.5 align-middle text-center">
                            <div class="text-xs text-slate-700">${inv.due_date || '-'}</div>
                            ${overdueText}
                        </td>

                        <!-- 8. Status -->
                        <td class="px-5 py-3.5 align-middle text-center">
                            <span class="inline-block px-2.5 py-0.5 rounded-full text-xs border ${badgeClass}">
                                ${inv.computed_status}
                            </span>
                        </td>

                        <!-- 9. Actions (Streamlined: View and Delete) -->
                        <td class="px-5 py-3.5 align-middle text-center whitespace-nowrap">
                            <div class="inline-flex items-center space-x-1">
                                <!-- View details button (primary access to view, edit, pay, convert) -->
                                <button type="button" onclick="openViewOrderModal(${inv.id})" class="p-1.5 text-blue-600 hover:text-blue-800 hover:bg-blue-50 rounded-md transition font-medium text-xs flex items-center space-x-1" title="View Full Details & Actions">
                                    <span>👁️</span>
                                    <span class="hidden sm:inline text-xs">View</span>
                                </button>

                                <!-- Delete button (finance/admin) -->
                                ${canManage ? `
                                    <button type="button" onclick="deleteInvoice(${inv.id})" class="p-1.5 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-md transition" title="Delete record">
                                        🗑️
                                    </button>
                                ` : ''}
                            </div>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        async function deleteInvoice(orderId) {
            const inv = cachedInvoices.find(i => i.id === orderId);
            const label = inv ? (inv.invoice_number ? `Invoice #${inv.invoice_number}` : (inv.po_number ? `PO #${inv.po_number}` : `Order #${orderId}`)) : `Order #${orderId}`;
            if (!confirm(`Are you sure you want to delete ${label}?\nThis will remove this record, its line items, and associated payment records permanently.`)) {
                return;
            }

            const res = await authFetch(`/api/invoices/${orderId}`, {
                method: 'DELETE'
            });

            if (res.ok) {
                fetchInvoices(currentInvoicePage);
                loadDashboard();
            } else {
                const err = await res.json();
                alert(err.detail || 'Failed to delete order/invoice');
            }
        }

        function openConvertModal(orderId) {
            const inv = cachedInvoices.find(i => i.id === orderId);
            const poNo = inv ? (inv.po_number || '') : '';
            const custName = inv ? (inv.customer_name || '') : '';
            document.getElementById('convertOrderId').value = orderId;
            document.getElementById('convertModalSubtitle').textContent = `PO: ${poNo || '#'+orderId} • Customer: ${custName}`;
            const today = new Date().toISOString().split('T')[0];
            document.getElementById('convertInvoiceDate').value = today;
            autoConvertDueDate();
            openModal('convertInvoiceModal');
        }

        function autoConvertDueDate() {
            const dStr = document.getElementById('convertInvoiceDate').value;
            if (dStr) {
                const d = new Date(dStr);
                d.setDate(d.getDate() + 30); // Net 30 default
                document.getElementById('convertDueDate').value = d.toISOString().split('T')[0];
            }
        }

        async function submitConvertToInvoice(e) {
            e.preventDefault();
            const orderId = document.getElementById('convertOrderId').value;
            const payload = {
                invoice_number: document.getElementById('convertInvoiceNo').value.trim(),
                invoice_date: document.getElementById('convertInvoiceDate').value,
                due_date: document.getElementById('convertDueDate').value || null
            };

            const res = await authFetch(`/api/invoices/${orderId}/convert-to-invoice`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                closeModal('convertInvoiceModal');
                document.getElementById('convertInvoiceForm').reset();
                fetchInvoices(currentInvoicePage);
                loadDashboard();
            } else {
                const err = await res.json();
                alert(err.detail || 'Failed to convert PO to Invoice');
            }
        }

        function calcTotalSettlement() {
            const cash = parseFloat(document.getElementById('payAmount').value) || 0;
            const tds = parseFloat(document.getElementById('payTDSAmount').value) || 0;
            const tot = cash + tds;
            document.getElementById('lblTotalSettlement').textContent = fmtCurr(tot);
        }

        function quickPay(id, bal) {
            document.getElementById('payInvoiceId').value = id;
            document.getElementById('payAmount').value = bal;
            document.getElementById('payTDSAmount').value = '0.00';
            document.getElementById('payTDSSection').value = '';
            calcTotalSettlement();
            openModal('paymentModal');
        }

        async function openViewOrderModal(orderId) {
            const res = await authFetch(`/api/invoices/${orderId}`);
            if (!res.ok) {
                alert('Failed to load order details');
                return;
            }
            const data = await res.json();
            const inv = data.invoice;
            const items = data.items || [];
            const payments = data.payments || [];

            const isUnbilled = inv.computed_status === 'UNBILLED';
            document.getElementById('viewModalTitle').textContent = inv.invoice_number ? `Invoice #${inv.invoice_number}` : `Order #${inv.id} (${inv.po_number || 'PO'})`;
            document.getElementById('viewModalSubtitle').textContent = `Financial Year: ${inv.fy_code} • Customer: ${inv.customer_name}`;

            let itemsHtml = items.length ? items.map((it, idx) => `
                <tr class="border-b border-slate-100">
                    <td class="py-2 text-slate-500 font-mono text-xs">${idx + 1}</td>
                    <td class="py-2 font-medium text-slate-900">${escapeHtml(it.description)}</td>
                    <td class="py-2 text-center text-slate-700">${it.quantity}</td>
                    <td class="py-2 text-right text-slate-700">${fmtCurr(it.unit_price)}</td>
                    <td class="py-2 text-center text-slate-500">${(it.tax_rate * 100).toFixed(0)}%</td>
                    <td class="py-2 text-right font-semibold text-slate-900">${fmtCurr(it.total_amount)}</td>
                </tr>
            `).join('') : `<tr><td colspan="6" class="py-3 text-center text-slate-400">No line items recorded</td></tr>`;

            let paymentsHtml = payments.length ? payments.map((p, idx) => `
                <tr class="border-b border-slate-100">
                    <td class="py-2 text-slate-700 font-medium">${p.payment_date}</td>
                    <td class="py-2 text-slate-600">${escapeHtml(p.payment_mode || 'BANK')}</td>
                    <td class="py-2 text-slate-500 font-mono">${escapeHtml(p.remittance_number || '-')}</td>
                    <td class="py-2 text-right text-emerald-600 font-semibold">${fmtCurr(p.payment_amount)}</td>
                    <td class="py-2 text-right text-amber-600 font-medium">${fmtCurr(p.tds_amount || 0)}</td>
                    <td class="py-2 text-right font-bold text-slate-900">${fmtCurr((p.payment_amount || 0) + (p.tds_amount || 0))}</td>
                </tr>
            `).join('') : `<tr><td colspan="6" class="py-3 text-center text-slate-400">No payment settlements logged yet</td></tr>`;

            const bodyHtml = `
                <!-- Top Summary Cards -->
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-slate-50 p-3.5 rounded-xl border border-slate-200">
                    <div>
                        <div class="text-[11px] font-semibold text-slate-500 uppercase">Total Invoiced</div>
                        <div class="text-base font-bold text-slate-900 mt-0.5">${fmtCurr(inv.total_amount)}</div>
                    </div>
                    <div>
                        <div class="text-[11px] font-semibold text-slate-500 uppercase">Paid / Settled</div>
                        <div class="text-base font-bold text-emerald-600 mt-0.5">${fmtCurr(inv.total_paid)}</div>
                    </div>
                    <div>
                        <div class="text-[11px] font-semibold text-slate-500 uppercase">Balance Due</div>
                        <div class="text-base font-bold ${inv.outstanding_balance > 0 ? 'text-amber-600' : 'text-slate-400'} mt-0.5">${fmtCurr(inv.outstanding_balance)}</div>
                    </div>
                    <div>
                        <div class="text-[11px] font-semibold text-slate-500 uppercase">Status</div>
                        <div class="text-xs font-bold mt-1">${escapeHtml(inv.computed_status)}</div>
                    </div>
                </div>

                <!-- Commercial & Customer Info -->
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 bg-white p-3 rounded-xl border border-slate-200 text-xs">
                    <div class="space-y-1.5">
                        <div><strong class="text-slate-700">Customer:</strong> <span class="text-slate-900 font-medium">${escapeHtml(inv.customer_name)}</span></div>
                        <div><strong class="text-slate-700">GSTIN:</strong> <span class="font-mono text-slate-600">${escapeHtml(inv.customer_gstin || 'None')}</span></div>
                        <div><strong class="text-slate-700">Address / State:</strong> <span class="text-slate-600">${escapeHtml(inv.customer_address || '-')} (${escapeHtml(inv.customer_state || '-')})</span></div>
                        <div><strong class="text-slate-700">Sales Representative:</strong> <span class="text-slate-900 font-semibold">${escapeHtml(inv.executive_name || 'Unassigned')}</span></div>
                    </div>
                    <div class="space-y-1.5">
                        <div><strong class="text-slate-700">Purchase Order:</strong> <span class="font-medium text-slate-900">${escapeHtml(inv.po_number || '-')}</span> ${inv.po_date ? `(${inv.po_date})` : ''}</div>
                        <div><strong class="text-slate-700">Invoice Number:</strong> <span class="font-mono text-slate-900 font-bold">${inv.invoice_number ? escapeHtml(inv.invoice_number) : '<span class="text-purple-600 font-normal italic">Unbilled PO</span>'}</span></div>
                        <div><strong class="text-slate-700">Invoice Date:</strong> <span class="text-slate-700">${inv.invoice_date || '-'}</span></div>
                        <div><strong class="text-slate-700">Payment Due Date:</strong> <span class="text-slate-700 font-medium">${inv.due_date || '-'}</span></div>
                        ${inv.remarks ? `<div><strong class="text-slate-700">Remarks:</strong> <span class="text-slate-600 italic">${escapeHtml(inv.remarks)}</span></div>` : ''}
                    </div>
                </div>

                <!-- Products & Services Line Items -->
                <div class="bg-white p-3 rounded-xl border border-slate-200 space-y-2">
                    <h4 class="text-xs font-bold text-slate-800 uppercase tracking-wide">Products & Services Line Items</h4>
                    <div class="overflow-x-auto">
                        <table class="min-w-full text-xs">
                            <thead class="text-slate-500 border-b border-slate-200">
                                <tr>
                                    <th class="text-left pb-1.5 font-semibold w-8">#</th>
                                    <th class="text-left pb-1.5 font-semibold">Description</th>
                                    <th class="text-center pb-1.5 font-semibold w-12">Qty</th>
                                    <th class="text-right pb-1.5 font-semibold w-24">Unit Price</th>
                                    <th class="text-center pb-1.5 font-semibold w-16">GST</th>
                                    <th class="text-right pb-1.5 font-semibold w-24">Total</th>
                                </tr>
                            </thead>
                            <tbody>${itemsHtml}</tbody>
                        </table>
                    </div>
                </div>

                <!-- Settlement & Payment Records -->
                <div class="bg-white p-3 rounded-xl border border-slate-200 space-y-2">
                    <h4 class="text-xs font-bold text-slate-800 uppercase tracking-wide">Settlement & Payment Records</h4>
                    <div class="overflow-x-auto">
                        <table class="min-w-full text-xs">
                            <thead class="text-slate-500 border-b border-slate-200">
                                <tr>
                                    <th class="text-left pb-1.5 font-semibold">Date</th>
                                    <th class="text-left pb-1.5 font-semibold">Mode</th>
                                    <th class="text-left pb-1.5 font-semibold">Reference</th>
                                    <th class="text-right pb-1.5 font-semibold">Cash (₹)</th>
                                    <th class="text-right pb-1.5 font-semibold">TDS (₹)</th>
                                    <th class="text-right pb-1.5 font-semibold">Settled (₹)</th>
                                </tr>
                            </thead>
                            <tbody>${paymentsHtml}</tbody>
                        </table>
                    </div>
                </div>
            `;

            document.getElementById('viewOrderBody').innerHTML = bodyHtml;

            // Render contextual actions inside view modal footer
            const actionsContainer = document.getElementById('viewOrderActions');
            const canManage = currentUser && (currentUser.role === 'admin' || currentUser.role === 'finance');
            let actionsHtml = '';

            if (canManage) {
                actionsHtml += `
                    <button type="button" onclick="closeModal('viewOrderModal'); openEditOrderModal(${inv.id});" class="text-xs bg-slate-100 text-slate-700 hover:bg-slate-200 px-3 py-1.5 rounded-lg border border-slate-300 font-semibold transition flex items-center space-x-1">
                        <span>✏️</span>
                        <span>Edit Order</span>
                    </button>
                `;
            }

            if (isUnbilled && canManage) {
                actionsHtml += `
                    <button type="button" onclick="closeModal('viewOrderModal'); openConvertModal(${inv.id});" class="text-xs bg-purple-600 text-white hover:bg-purple-500 px-3 py-1.5 rounded-lg font-semibold shadow-sm transition flex items-center space-x-1">
                        <span>+</span>
                        <span>Issue Invoice</span>
                    </button>
                `;
            }

            if (inv.outstanding_balance > 0) {
                actionsHtml += `
                    <button type="button" onclick="closeModal('viewOrderModal'); quickPay(${inv.id}, ${inv.outstanding_balance});" class="text-xs bg-emerald-600 text-white hover:bg-emerald-500 px-3 py-1.5 rounded-lg font-semibold shadow-sm transition flex items-center space-x-1">
                        <span>₹</span>
                        <span>Record Payment</span>
                    </button>
                `;
            }

            actionsContainer.innerHTML = actionsHtml;
            openModal('viewOrderModal');
        }

        async function openEditOrderModal(orderId) {
            const res = await authFetch(`/api/invoices/${orderId}`);
            if (!res.ok) {
                alert('Failed to load order details');
                return;
            }
            const data = await res.json();
            const inv = data.invoice;
            const isIssued = Boolean(inv.invoice_number && inv.invoice_number.trim());

            document.getElementById('editOrderId').value = inv.id;
            document.getElementById('editModalTitle').textContent = isIssued ? `Edit Invoice #${inv.invoice_number}` : `Edit PO #${inv.po_number || inv.id}`;

            // Populate Customer dropdown
            const custSelect = document.getElementById('editCustomer');
            custSelect.innerHTML = '<option value="">-- Select Customer --</option>';
            cachedCustomers.forEach(c => {
                const sel = c.id === inv.customer_id ? 'selected' : '';
                custSelect.innerHTML += `<option value="${c.id}" ${sel}>${escapeHtml(c.billing_name)}</option>`;
            });

            // Populate Executive dropdown (sales role users)
            const execSelect = document.getElementById('editExecutive');
            const metaRes = await authFetch('/api/meta');
            if (metaRes.ok) {
                const meta = await metaRes.json();
                execSelect.innerHTML = '<option value="">-- Unassigned --</option>';
                (meta.executives || []).forEach(e => {
                    const sel = e.id === inv.executive_id ? 'selected' : '';
                    execSelect.innerHTML += `<option value="${e.id}" ${sel}>${escapeHtml(e.name)}</option>`;
                });
            }

            document.getElementById('editPONo').value = inv.po_number || '';
            document.getElementById('editPODate').value = inv.po_date || '';
            document.getElementById('editDueDate').value = inv.due_date || '';
            document.getElementById('editRemarks').value = inv.remarks || '';

            // Render line items for editable description
            const itemsContainer = document.getElementById('editLineItemsContainer');
            const items = data.items || [];
            if (itemsContainer) {
                if (items.length) {
                    itemsContainer.innerHTML = items.map((it, idx) => `
                        <div class="flex items-center space-x-2">
                            <span class="text-xs font-mono text-slate-400 w-5 text-right shrink-0">${idx + 1}.</span>
                            <input type="text" data-item-id="${it.id}" value="${escapeHtml(it.description || '')}"
                                   class="edit-item-desc flex-1 px-2.5 py-1.5 border border-slate-300 rounded text-xs bg-white focus:outline-none focus:ring-1 focus:ring-blue-500"
                                   placeholder="Item or service description...">
                            <span class="text-[11px] text-slate-500 font-medium shrink-0">${fmtCurr(it.total_amount)}</span>
                        </div>
                    `).join('');
                } else {
                    itemsContainer.innerHTML = '<p class="text-xs text-slate-400 italic">No line items recorded</p>';
                }
            }

            // Handle Immutability toggling
            const note = document.getElementById('editImmutabilityNote');
            if (isIssued) {
                note.classList.remove('hidden');
                document.getElementById('editCustomer').disabled = true;
                document.getElementById('editPONo').disabled = true;
                document.getElementById('editPODate').disabled = true;
            } else {
                note.classList.add('hidden');
                document.getElementById('editCustomer').disabled = false;
                document.getElementById('editPONo').disabled = false;
                document.getElementById('editPODate').disabled = false;
            }

            openModal('editOrderModal');
        }

        async function submitEditOrder(e) {
            e.preventDefault();
            const orderId = document.getElementById('editOrderId').value;
            const execVal = document.getElementById('editExecutive').value;
            const custVal = document.getElementById('editCustomer').value;

            // Collect line item descriptions
            const itemInputs = document.querySelectorAll('.edit-item-desc');
            const updatedItems = Array.from(itemInputs).map(inp => ({
                id: parseInt(inp.getAttribute('data-item-id')),
                description: inp.value.trim()
            }));

            const payload = {
                remarks: document.getElementById('editRemarks').value,
                due_date: document.getElementById('editDueDate').value || null,
                executive_id: execVal ? parseInt(execVal) : null,
                customer_id: custVal ? parseInt(custVal) : null,
                po_number: document.getElementById('editPONo').value || null,
                po_date: document.getElementById('editPODate').value || null,
                items: updatedItems.length ? updatedItems : null
            };

            const res = await authFetch(`/api/invoices/${orderId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                closeModal('editOrderModal');
                fetchInvoices(currentInvoicePage);
                loadDashboard();
            } else {
                const err = await res.json();
                alert(err.detail || 'Failed to update order');
            }
        }

        function prevInvoicePage() { if (currentInvoicePage > 1) fetchInvoices(currentInvoicePage - 1); }
        function nextInvoicePage() { fetchInvoices(currentInvoicePage + 1); }
        function debounceFetchInvoices() {
            clearTimeout(invoiceDebounceTimer);
            invoiceDebounceTimer = setTimeout(() => fetchInvoices(1), 300);
        }

        async function fetchCustomers() {
            const q = document.getElementById('customerSearch').value;
            const res = await authFetch(`/api/customers?search=${encodeURIComponent(q)}&limit=500`);
            if (!res.ok) return;
            const custs = await res.json();
            cachedCustomers = custs || [];
            const countBadge = document.getElementById('customerCountBadge');
            if (countBadge) countBadge.textContent = `${cachedCustomers.length.toLocaleString()} customers`;

            const tb = document.getElementById('customerTableBody');
            const canManage = currentUser && (currentUser.role === 'admin' || currentUser.role === 'finance');
            if (!cachedCustomers.length) {
                tb.innerHTML = `<tr><td colspan="7" class="px-6 py-12 text-center text-slate-400">No matching customers found</td></tr>`;
                return;
            }
            tb.innerHTML = cachedCustomers.map(c => `
                <tr class="hover:bg-slate-50/80 transition group">
                    <!-- Customer & Tax Identity -->
                    <td class="px-5 py-3.5 align-top">
                        <div class="font-semibold text-slate-900">${escapeHtml(c.billing_name)}</div>
                        <div class="mt-1 flex items-center space-x-2 text-xs">
                            ${c.gst_number ? `
                                <span class="font-mono text-[11px] bg-slate-100 text-slate-700 px-1.5 py-0.5 rounded font-medium">${escapeHtml(c.gst_number)}</span>
                            ` : `<span class="text-slate-400 italic">No GSTIN registered</span>`}
                        </div>
                    </td>

                    <!-- Location -->
                    <td class="px-5 py-3.5 align-top">
                        <div class="text-sm text-slate-700 font-medium">${escapeHtml(c.city || '-')}</div>
                        <div class="text-xs text-slate-400 mt-0.5">${escapeHtml(c.state || '')}</div>
                    </td>

                    <!-- Contact Info -->
                    <td class="px-5 py-3.5 align-top">
                        <div class="text-sm font-medium text-slate-800">${escapeHtml(c.contact_person || '-')}</div>
                        <div class="text-xs text-slate-400 mt-0.5 flex flex-col space-y-0.5">
                            ${c.phone_1 ? `<span>📞 ${escapeHtml(c.phone_1)}</span>` : ''}
                            ${c.email ? `<span class="truncate max-w-[180px]">✉️ ${escapeHtml(c.email)}</span>` : ''}
                        </div>
                    </td>

                    <!-- Orders -->
                    <td class="px-5 py-3.5 align-top text-right">
                        <span class="inline-block bg-slate-100 text-slate-700 text-xs font-semibold px-2.5 py-1 rounded-full">
                            ${c.total_orders}
                        </span>
                    </td>

                    <!-- Lifetime Billed -->
                    <td class="px-5 py-3.5 align-top text-right">
                        <div class="font-semibold text-slate-900">${fmtCurr(c.total_billed)}</div>
                        <div class="text-[11px] text-emerald-600 font-medium mt-0.5">Paid: ${fmtCurr(c.total_paid || 0)}</div>
                    </td>

                    <!-- Outstanding Balance -->
                    <td class="px-5 py-3.5 align-top text-right">
                        <div class="font-bold ${c.total_outstanding > 0 ? 'text-amber-600' : 'text-slate-400'}">
                            ${fmtCurr(c.total_outstanding)}
                        </div>
                    </td>

                    <!-- Actions -->
                    <td class="px-5 py-3.5 align-top text-center">
                        ${canManage ? `
                            <div class="flex items-center justify-center space-x-1.5">
                                <button onclick="openEditCustomerModal(${c.id})" title="Edit Customer Details" class="p-1.5 rounded-md text-slate-400 hover:text-blue-600 hover:bg-blue-50 transition">
                                    ✏️
                                </button>
                                <button onclick="confirmDeleteCustomer(${c.id})" title="Delete Customer" class="p-1.5 rounded-md text-slate-400 hover:text-rose-600 hover:bg-rose-50 transition">
                                    🗑️
                                </button>
                            </div>
                        ` : `
                            <span class="text-[11px] text-slate-400 font-normal italic">Read-only</span>
                        `}
                    </td>
                </tr>
            `).join('');
        }

        function debounceFetchCustomers() {
            clearTimeout(customerDebounceTimer);
            customerDebounceTimer = setTimeout(fetchCustomers, 300);
        }

        
        function setBillingType(type) {
            document.getElementById('formOrderType').value = type;
            const btnPO = document.getElementById('btnTypePO');
            const btnRec = document.getElementById('btnTypeRecurring');
            const poInput = document.getElementById('formPONo');
            const lblPO = document.getElementById('lblPONo');
            const poDateInput = document.getElementById('formPODate');
            const lblPODate = document.getElementById('lblPODate');
            const divTopDue = document.getElementById('divTopDueDate');
            const secPOBilling = document.getElementById('secPOBillingStatus');

            if (type === 'PO') {
                btnPO.className = 'flex-1 py-1.5 text-xs font-semibold rounded-md bg-white text-slate-800 shadow-sm transition';
                btnRec.className = 'flex-1 py-1.5 text-xs font-medium rounded-md text-slate-500 hover:text-slate-800 transition';
                lblPO.textContent = 'Purchase Order No *';
                poInput.required = true;
                poInput.placeholder = 'e.g. PO/2026/099';

                lblPODate.textContent = 'PO Date';
                poDateInput.onchange = null;

                if (divTopDue) divTopDue.classList.add('hidden');
                if (secPOBilling) secPOBilling.classList.remove('hidden');
            } else {
                btnRec.className = 'flex-1 py-1.5 text-xs font-semibold rounded-md bg-white text-slate-800 shadow-sm transition';
                btnPO.className = 'flex-1 py-1.5 text-xs font-medium rounded-md text-slate-500 hover:text-slate-800 transition';
                lblPO.textContent = 'Invoice Number *';
                poInput.required = true;
                poInput.placeholder = 'e.g. INV/2026/001';

                lblPODate.textContent = 'Invoice Date *';
                poDateInput.required = true;
                poDateInput.onchange = autoRecurringDueDate;

                if (divTopDue) divTopDue.classList.remove('hidden');
                if (secPOBilling) secPOBilling.classList.add('hidden');

                // If date already present, calculate due date immediately
                if (poDateInput.value) autoRecurringDueDate();
            }
        }

        function autoRecurringDueDate() {
            const invD = document.getElementById('formPODate').value;
            if (invD) {
                const d = new Date(invD);
                d.setDate(d.getDate() + 30);
                document.getElementById('formTopDueDate').value = d.toISOString().split('T')[0];
            }
        }

        function autoPODueDate() {
            const invD = document.getElementById('formPOInvoiceDate').value;
            if (invD) {
                const d = new Date(invD);
                d.setDate(d.getDate() + 30);
                document.getElementById('formPODueDate').value = d.toISOString().split('T')[0];
            }
        }

        
        async function submitCreateInvoice(e) {
            e.preventDefault();
            const curr = document.getElementById('formCurrency').value;
            const rate = curr === 'INR' ? 1.0 : (parseFloat(document.getElementById('formExchangeRate').value) || 1.0);

            // Collect line items
            const items = [];
            document.querySelectorAll('#lineItemsTableBody tr').forEach(r => {
                const desc = r.querySelector('.item-desc').value.trim();
                const qty = parseFloat(r.querySelector('.item-qty').value) || 1;
                const price = parseFloat(r.querySelector('.item-price').value) || 0;
                const taxRate = parseFloat(r.querySelector('.item-tax').value) || 0;
                const lineExcl = qty * price;
                const lineTax = Math.round(lineExcl * taxRate * 100) / 100;
                const lineTot = lineExcl + lineTax;
                if (desc) {
                    items.push({
                        description: desc,
                        quantity: qty,
                        unit_price: price,
                        tax_rate: taxRate,
                        tax_amount: lineTax,
                        total_amount: lineTot
                    });
                }
            });

            if (items.length === 0) {
                alert('Please enter at least one product or service line item.');
                return;
            }

            const baseExclINR = parseFloat(document.getElementById('formExcl').value) || 0;
            const baseTaxINR = parseFloat(document.getElementById('formTaxAmt').value) || 0;
            const baseTotalINR = parseFloat(document.getElementById('formTotalAmt').value) || 0;

            const execSelect = document.getElementById('formExecutive');
            const execName = execSelect.options[execSelect.selectedIndex] ? execSelect.options[execSelect.selectedIndex].text : null;

            const orderType = document.getElementById('formOrderType').value;
            let poNum = null;
            let poDt = null;
            let invNum = null;
            let invDt = null;
            let dueDt = null;

            if (orderType === 'RECURRING') {
                invNum = document.getElementById('formPONo').value.trim() || null;
                invDt = document.getElementById('formPODate').value || null;
                dueDt = document.getElementById('formTopDueDate').value || null;
                poNum = null;
                poDt = null;
            } else {
                poNum = document.getElementById('formPONo').value.trim() || null;
                poDt = document.getElementById('formPODate').value || null;
                invNum = document.getElementById('formPOInvoiceNo').value.trim() || null;
                invDt = document.getElementById('formPOInvoiceDate').value || null;
                dueDt = document.getElementById('formPODueDate').value || null;
            }

            const payload = {
                fy_code: document.getElementById('formFY').value,
                order_type: orderType,
                customer_id: parseInt(document.getElementById('formCustomer').value),
                executive_name: execName,
                currency: curr,
                exchange_rate: rate,
                original_amount: parseFloat(document.getElementById('sumSubtotal').textContent) || 0,
                exclusive_amount: baseExclINR,
                tax_rate: 0.18,
                tax_amount: baseTaxINR,
                total_amount: baseTotalINR,
                po_number: poNum,
                po_date: poDt,
                invoice_number: invNum,
                invoice_date: invDt,
                due_date: dueDt,
                items: items
            };

            const res = await authFetch('/api/invoices', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                closeModal('invoiceModal');
                document.getElementById('createInvoiceForm').reset();
                document.getElementById('lineItemsTableBody').innerHTML = '';
                setBillingType('PO');
                handleCurrencyChange();
                fetchInvoices(1);
                loadDashboard();
            } else {
                const err = await res.json();
                alert(err.detail || 'Failed to create invoice');
            }
        }


        async function submitRecordPayment(e) {
            e.preventDefault();
            const payload = {
                invoice_id: parseInt(document.getElementById('payInvoiceId').value),
                payment_amount: parseFloat(document.getElementById('payAmount').value) || 0,
                tds_amount: parseFloat(document.getElementById('payTDSAmount').value) || 0,
                tds_section: document.getElementById('payTDSSection').value || null,
                payment_date: document.getElementById('payDate').value,
                payment_mode: document.getElementById('payMode').value,
                remittance_number: document.getElementById('payRemit').value || null
            };
            const res = await authFetch('/api/payments', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                closeModal('paymentModal');
                document.getElementById('recordPaymentForm').reset();
                fetchInvoices(currentInvoicePage);
                loadDashboard();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to record payment');
            }
        }

        
        async function submitSaveCustomer(e) {
            e.preventDefault();
            const editId = document.getElementById('custEditId').value;
            const payload = {
                billing_name: document.getElementById('custName').value.trim(),
                gst_number: document.getElementById('custGST').value.trim() || null,
                city: document.getElementById('custCity').value.trim() || null,
                contact_person: document.getElementById('custPerson').value.trim() || null,
                phone_1: document.getElementById('custPhone').value.trim() || null
            };

            const url = editId ? `/api/customers/${editId}` : '/api/customers';
            const method = editId ? 'PUT' : 'POST';

            const res = await authFetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                closeModal('customerModal');
                document.getElementById('createCustomerForm').reset();
                document.getElementById('custEditId').value = '';
                initApp();
                fetchCustomers();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to save customer');
            }
        }

        async function openEditCustomerModal(id) {
            const res = await authFetch(`/api/customers/${id}`);
            if (!res.ok) {
                alert('Failed to fetch customer details');
                return;
            }
            const data = await res.json();
            const c = data.customer;

            document.getElementById('custEditId').value = c.id;
            document.getElementById('custName').value = c.billing_name || '';
            document.getElementById('custGST').value = c.gst_number || '';
            document.getElementById('custCity').value = c.city || '';
            document.getElementById('custPerson').value = c.contact_person || '';
            document.getElementById('custPhone').value = c.phone_1 || '';

            document.getElementById('customerModalTitle').textContent = `Edit Customer: ${c.billing_name}`;
            document.getElementById('custModalSubmitBtn').textContent = 'Update Customer';
            document.getElementById('custModalSubmitBtn').className = 'px-5 py-2 bg-emerald-600 hover:bg-emerald-500 text-white font-medium rounded-lg text-sm shadow';

            openModal('customerModal');
        }

        async function confirmDeleteCustomer(id) {
            const c = cachedCustomers.find(item => item.id === id);
            const name = c ? c.billing_name : 'Customer';
            const totalOrders = c ? (c.total_orders || 0) : 0;
            if (totalOrders > 0) {
                alert(`Cannot delete '${name}' because they have ${totalOrders} associated order(s)/invoice(s). Delete or reassign those orders first.`);
                return;
            }
            if (!confirm(`Are you sure you want to permanently delete customer '${name}'?`)) {
                return;
            }

            const res = await authFetch(`/api/customers/${id}`, {
                method: 'DELETE'
            });

            if (res.ok) {
                initApp();
                fetchCustomers();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to delete customer');
            }
        }

        async function submitCreateExecutive(e) {
            e.preventDefault();
            const payload = {
                name: document.getElementById('execName').value.trim(),
                email: document.getElementById('execEmail').value.trim() || null
            };
            const res = await fetch('/api/meta/executives', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                closeModal('executiveModal');
                initApp();
            } else {
                alert('Failed to save executive');
            }
        }

        async function promptAddFY() {
            const fy = prompt("Enter Financial Year code (e.g. 'FY 2026-27'):", "FY 2026-27");
            if (fy && fy.trim()) {
                await fetch('/api/meta/financial-years', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code: fy.trim() })
                });
                initApp();
            }
        }

        
        // --- Authentication Management ---
        async function checkAuth() {
            const token = localStorage.getItem('cf_auth_token');
            if (!token) {
                showLogin();
                return;
            }
            try {
                const res = await fetch('/api/auth/me', {
                    headers: { 'Authorization': `Bearer ${token}` }
                });
                if (res.ok) {
                    const data = await res.json();
                    hideLogin(data.user);
                    initApp();
                } else {
                    localStorage.removeItem('cf_auth_token');
                    showLogin();
                }
            } catch (err) {
                showLogin();
            }
        }

        function showLogin() {
            document.getElementById('authScreen').classList.remove('hidden');
            document.getElementById('userHeaderBadge').classList.add('hidden');
        }

        function hideLogin(user) {
            currentUser = user;
            document.getElementById('authScreen').classList.add('hidden');
            const badge = document.getElementById('userHeaderBadge');
            badge.classList.remove('hidden');
            badge.classList.add('flex');
            document.getElementById('userNameText').textContent = `${user.full_name || user.username} (${user.role})`;
        }

        async function handleLogin(e) {
            e.preventDefault();
            const u = document.getElementById('loginUsername').value.trim();
            const p = document.getElementById('loginPassword').value;
            const errDiv = document.getElementById('loginError');
            errDiv.classList.add('hidden');

            const res = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: u, password: p })
            });
            const data = await res.json();
            if (res.ok && data.token) {
                localStorage.setItem('cf_auth_token', data.token);
                hideLogin(data.user);
                initApp();
            } else {
                errDiv.textContent = data.detail || 'Login failed';
                errDiv.classList.remove('hidden');
            }
        }

        async function handleLogout() {
            const token = localStorage.getItem('cf_auth_token');
            if (token) {
                await fetch('/api/auth/logout', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${token}` }
                });
            }
            localStorage.removeItem('cf_auth_token');
            showLogin();
        }

        
        async function fetchUsers() {
            const token = localStorage.getItem('cf_auth_token');
            const tb = document.getElementById('usersTableBody');
            if (!token) {
                tb.innerHTML = `<tr><td colspan="5" class="px-6 py-6 text-center text-slate-400">Please sign in to view team members.</td></tr>`;
                return;
            }
            const res = await fetch('/api/users', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.status === 401) {
                localStorage.removeItem('cf_auth_token');
                showLogin();
                return;
            }
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                tb.innerHTML = `<tr><td colspan="5" class="px-6 py-6 text-center text-rose-500">${err.detail || 'Failed to load user accounts.'}</td></tr>`;
                return;
            }
            cachedUsers = await res.json() || [];
            tb.innerHTML = cachedUsers.map(u => {
                let roleColor = 'bg-slate-100 text-slate-700';
                if (u.role === 'admin') roleColor = 'bg-purple-100 text-purple-800 font-semibold';
                else if (u.role === 'finance') roleColor = 'bg-blue-100 text-blue-800';
                else if (u.role === 'sales') roleColor = 'bg-emerald-100 text-emerald-800';

                return `
                    <tr class="hover:bg-slate-50 transition">
                        <td class="px-6 py-3.5 font-medium text-slate-900">${escapeHtml(u.full_name || u.username)}</td>
                        <td class="px-6 py-3.5 font-mono text-xs text-slate-600">${escapeHtml(u.username)}</td>
                        <td class="px-6 py-3.5 text-center"><span class="px-2.5 py-0.5 rounded-full text-xs uppercase tracking-wider ${roleColor}">${escapeHtml(u.role)}</span></td>
                        <td class="px-6 py-3.5 text-xs text-slate-500">${escapeHtml(u.created_at || '-')}</td>
                        <td class="px-6 py-3.5 text-center space-x-2">
                            ${u.role === 'sales' ? `<button onclick="openTargetModal(${u.id})" class="text-xs bg-purple-50 text-purple-700 hover:bg-purple-100 px-2 py-1 rounded border border-purple-200 font-medium transition">🎯 Quotas</button>` : ''}
                            <button onclick="promptResetPw(${u.id})" class="text-xs text-blue-600 hover:underline">Reset Pw</button>
                            ${u.id !== 1 ? `<button onclick="deleteUser(${u.id})" class="text-xs text-rose-600 hover:underline">Delete</button>` : `<span class="text-xs text-slate-400">Primary Admin</span>`}
                        </td>
                    </tr>
                `;
            }).join('');
        }

        async function submitCreateUser(e) {
            e.preventDefault();
            const errDiv = document.getElementById('createUserError');
            errDiv.classList.add('hidden');

            const payload = {
                full_name: document.getElementById('newFullName').value.trim() || null,
                username: document.getElementById('newUsername').value.trim(),
                password: document.getElementById('newUserPassword').value,
                role: document.getElementById('newUserRole').value
            };

            const res = await authFetch('/api/users', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                closeModal('addUserModal');
                document.getElementById('createUserForm').reset();
                fetchUsers();
            } else {
                const err = await res.json();
                errDiv.textContent = err.detail || 'Failed to create user';
                errDiv.classList.remove('hidden');
            }
        }

        async function deleteUser(id) {
            const u = cachedUsers.find(item => item.id === id);
            const uname = u ? u.username : `User #${id}`;
            if (!confirm(`Are you sure you want to delete user '${uname}'?`)) return;
            const res = await authFetch(`/api/users/${id}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                fetchUsers();
            } else {
                const err = await res.json();
                alert(err.detail || 'Failed to delete user');
            }
        }

        async function promptResetPw(id) {
            const u = cachedUsers.find(item => item.id === id);
            const uname = u ? u.username : `User #${id}`;
            const newPw = prompt(`Enter new password for '${uname}' (min 6 chars):`);
            if (!newPw) return;
            const res = await authFetch(`/api/users/${id}/reset-password`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ new_password: newPw })
            });
            if (res.ok) {
                alert(`Password updated for user '${uname}'`);
            } else {
                const err = await res.json();
                alert(err.detail || 'Failed to reset password');
            }
        }

        async function handleChangePassword(e) {
            e.preventDefault();
            const msg = document.getElementById('changePwMsg');
            msg.className = 'hidden p-3 rounded-lg text-xs font-medium';

            const payload = {
                current_password: document.getElementById('curPassword').value,
                new_password: document.getElementById('newPassword').value
            };

            const res = await authFetch('/api/auth/change-password', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                msg.textContent = 'Password changed successfully!';
                msg.className = 'p-3 bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-lg text-xs font-medium';
                document.getElementById('changePasswordForm').reset();
            } else {
                const err = await res.json();
                msg.textContent = err.detail || 'Failed to change password';
                msg.className = 'p-3 bg-rose-50 text-rose-700 border border-rose-200 rounded-lg text-xs font-medium';
            }
        }

        
        // --- Line Items Dynamic Engine ---
        let lineItemCount = 0;

        function renderTaxOptions(selectedRate = 0.18) {
            if (!cachedTaxRates || cachedTaxRates.length === 0) {
                return `
                    <option value="0.18" ${Math.abs(selectedRate - 0.18) < 0.001 ? 'selected' : ''}>18% GST</option>
                    <option value="0.28" ${Math.abs(selectedRate - 0.28) < 0.001 ? 'selected' : ''}>28% GST</option>
                    <option value="0.12" ${Math.abs(selectedRate - 0.12) < 0.001 ? 'selected' : ''}>12% GST</option>
                    <option value="0.05" ${Math.abs(selectedRate - 0.05) < 0.001 ? 'selected' : ''}>5% GST</option>
                    <option value="0.00" ${Math.abs(selectedRate - 0.00) < 0.001 ? 'selected' : ''}>0% (Exempt)</option>
                `;
            }
            return cachedTaxRates.map(t => {
                const pct = (t.rate * 100).toFixed(t.rate * 100 % 1 === 0 ? 0 : 1);
                const isSel = Math.abs(t.rate - selectedRate) < 0.001 ? 'selected' : '';
                return `<option value="${t.rate}" ${isSel}>${pct}% (${t.tax_type})</option>`;
            }).join('');
        }

        function addLineItemRow(desc = '', qty = 1, price = 0, taxRate = 0.18) {
            lineItemCount++;
            const rowId = `item-row-${lineItemCount}`;
            const tb = document.getElementById('lineItemsTableBody');
            const tr = document.createElement('tr');
            tr.id = rowId;
            tr.className = 'hover:bg-white/80 transition';
            tr.innerHTML = `
                <td class="py-2 pr-2">
                    <input type="text" class="item-desc w-full px-2 py-1.5 border border-slate-300 rounded text-xs" required placeholder="e.g. Cloud Subscription / Dev Service" value="${escapeHtml(desc)}">
                </td>
                <td class="py-2 px-1 text-center">
                    <input type="number" step="1" min="1" class="item-qty w-16 px-2 py-1.5 border border-slate-300 rounded text-xs text-center" value="${qty}" oninput="calcLineItems()">
                </td>
                <td class="py-2 px-1 text-right">
                    <input type="number" step="0.01" min="0" class="item-price w-24 px-2 py-1.5 border border-slate-300 rounded text-xs text-right" value="${price}" oninput="calcLineItems()">
                </td>
                <td class="py-2 px-1 text-center">
                    <select class="item-tax w-24 px-1 py-1.5 border border-slate-300 rounded text-xs bg-white text-center" onchange="calcLineItems()">
                        ${renderTaxOptions(taxRate)}
                    </select>
                </td>
                <td class="py-2 pl-2 text-right font-medium text-slate-800 item-row-total">
                    0.00
                </td>
                <td class="py-2 pl-2 text-center">
                    <button type="button" onclick="removeLineItemRow('${rowId}')" class="text-rose-500 hover:text-rose-700 font-bold text-sm px-1.5 py-0.5 rounded">&times;</button>
                </td>
            `;
            tb.appendChild(tr);
            calcLineItems();
        }

        function removeLineItemRow(rowId) {
            const rows = document.querySelectorAll('#lineItemsTableBody tr');
            if (rows.length <= 1) {
                alert('Invoice must have at least one product or service line item.');
                return;
            }
            const r = document.getElementById(rowId);
            if (r) r.remove();
            calcLineItems();
        }

        function calcLineItems() {
            const curr = document.getElementById('formCurrency').value;
            const rate = curr === 'INR' ? 1.0 : (parseFloat(document.getElementById('formExchangeRate').value) || 1.0);

            let totalExclDeal = 0;
            let totalTaxDeal = 0;

            const rows = document.querySelectorAll('#lineItemsTableBody tr');
            rows.forEach(r => {
                const qty = parseFloat(r.querySelector('.item-qty').value) || 0;
                const price = parseFloat(r.querySelector('.item-price').value) || 0;
                const taxRate = parseFloat(r.querySelector('.item-tax').value) || 0;

                const lineExcl = qty * price;
                const lineTax = Math.round(lineExcl * taxRate * 100) / 100;
                const lineTot = lineExcl + lineTax;

                r.querySelector('.item-row-total').textContent = lineTot.toFixed(2);
                totalExclDeal += lineExcl;
                totalTaxDeal += lineTax;
            });

            const grandDeal = totalExclDeal + totalTaxDeal;

            document.getElementById('sumSubtotal').textContent = totalExclDeal.toFixed(2);
            document.getElementById('sumTax').textContent = totalTaxDeal.toFixed(2);
            document.getElementById('sumGrandTotal').textContent = grandDeal.toFixed(2);

            // Convert to INR Base
            const baseExclINR = Math.round(totalExclDeal * rate * 100) / 100;
            const baseTaxINR = Math.round(totalTaxDeal * rate * 100) / 100;
            const baseTotalINR = Math.round(grandDeal * rate * 100) / 100;

            document.getElementById('formExcl').value = baseExclINR;
            document.getElementById('formTaxAmt').value = baseTaxINR;
            document.getElementById('formTotalAmt').value = baseTotalINR;

            if (curr !== 'INR') {
                document.getElementById('inrConvertedText').textContent = fmtCurr(baseTotalINR);
            }
        }

        function openCustomerModalFresh() {
            document.getElementById('custEditId').value = '';
            document.getElementById('customerModalTitle').textContent = 'Add New Customer';
            document.getElementById('custModalSubmitBtn').textContent = 'Save Customer';
            document.getElementById('custModalSubmitBtn').className = 'px-5 py-2 bg-blue-600 hover:bg-blue-500 text-white font-medium rounded-lg text-sm shadow';
            document.getElementById('createCustomerForm').reset();
            openModal('customerModal');
        }

        // --- Settings Sidebar Sub-Tab Engine ---
        function switchSettingsSection(sectionId) {
            const sections = ['users', 'fy', 'taxes', 'password'];
            sections.forEach(s => {
                const secEl = document.getElementById(`setSection-${s}`);
                const btnEl = document.getElementById(`setNav-${s}`);
                if (secEl) secEl.classList.add('hidden');
                if (btnEl) {
                    btnEl.className = 'w-full flex items-center space-x-3 px-3 py-2.5 rounded-lg text-xs font-medium text-slate-600 hover:bg-slate-50 transition text-left';
                    const subText = btnEl.querySelector('.text-\\[10px\\]');
                    if (subText) subText.className = 'text-[10px] text-slate-400 font-normal';
                }
            });

            const targetSec = document.getElementById(`setSection-${sectionId}`);
            const targetBtn = document.getElementById(`setNav-${sectionId}`);
            if (targetSec) targetSec.classList.remove('hidden');
            if (targetBtn) {
                targetBtn.className = 'w-full flex items-center space-x-3 px-3 py-2.5 rounded-lg text-xs font-semibold text-blue-700 bg-blue-50/80 transition text-left';
                const subText = targetBtn.querySelector('.text-\\[10px\\]');
                if (subText) subText.className = 'text-[10px] text-blue-600 font-normal';
            }
        }

        // --- Financial Years & Tax Structure Master Functions ---
        async function fetchSettingsMasters() {
            try { await fetchUsers(); } catch (e) { console.error('fetchUsers error:', e); }
            try { await fetchFYs(); } catch (e) { console.error('fetchFYs error:', e); }
            try { await fetchTaxMasters(); } catch (e) { console.error('fetchTaxMasters error:', e); }
        }

        async function fetchTaxMasters() {
            const res = await authFetch('/api/settings/taxes');
            if (!res.ok) return;
            const data = await res.json();
            
            // Render GST table
            const gstTb = document.getElementById('gstTableBody');
            if (!data.gst_rates || !data.gst_rates.length) {
                gstTb.innerHTML = `<tr><td colspan="5" class="px-3 py-3 text-center text-slate-400">No GST slabs configured.</td></tr>`;
            } else {
                gstTb.innerHTML = data.gst_rates.map(g => {
                    const pct = (g.rate * 100).toFixed(g.rate * 100 % 1 === 0 ? 0 : 1);
                    return `
                        <tr class="hover:bg-slate-50 transition">
                            <td class="px-3 py-2 font-medium text-slate-800">${escapeHtml(g.name)}</td>
                            <td class="px-3 py-2 text-center font-bold text-blue-600">${pct}%</td>
                            <td class="px-3 py-2 text-center"><span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-100 text-slate-700">${escapeHtml(g.tax_type)}</span></td>
                            <td class="px-3 py-2 text-center">${g.is_default ? '<span class="text-emerald-600 font-bold">✓ Yes</span>' : '<span class="text-slate-400">-</span>'}</td>
                            <td class="px-3 py-2 text-center">
                                <button onclick="deleteGST(${g.id})" class="text-rose-600 hover:underline">Delete</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }

            // Render TDS table
            const tdsTb = document.getElementById('tdsTableBody');
            if (!data.tds_rates || !data.tds_rates.length) {
                tdsTb.innerHTML = `<tr><td colspan="4" class="px-3 py-3 text-center text-slate-400">No TDS sections configured.</td></tr>`;
            } else {
                tdsTb.innerHTML = data.tds_rates.map(t => {
                    const pct = (t.rate * 100).toFixed(t.rate * 100 % 1 === 0 ? 0 : 1);
                    return `
                        <tr class="hover:bg-slate-50 transition">
                            <td class="px-3 py-2 font-bold text-slate-800 font-mono">${escapeHtml(t.section_code)}</td>
                            <td class="px-3 py-2 text-slate-600">${escapeHtml(t.description || '-')}</td>
                            <td class="px-3 py-2 text-center font-bold text-indigo-600">${pct}%</td>
                            <td class="px-3 py-2 text-center">
                                <button onclick="deleteTDS(${t.id})" class="text-rose-600 hover:underline">Delete</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }

        async function submitCreateGST(e) {
            e.preventDefault();
            const payload = {
                name: document.getElementById('gstName').value.trim(),
                rate: parseFloat(document.getElementById('gstRate').value),
                tax_type: document.getElementById('gstType').value,
                is_default: document.getElementById('gstIsDefault').checked ? 1 : 0
            };
            const res = await authFetch('/api/settings/taxes/gst', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                closeModal('addGSTModal');
                document.getElementById('createGSTForm').reset();
                fetchTaxMasters();
                initApp();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to add GST slab');
            }
        }

        async function deleteGST(id) {
            if (!confirm('Are you sure you want to deactivate this GST slab?')) return;
            const res = await authFetch(`/api/settings/taxes/gst/${id}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                fetchTaxMasters();
                initApp();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to deactivate GST slab');
            }
        }

        async function submitCreateTDS(e) {
            e.preventDefault();
            const payload = {
                section_code: document.getElementById('tdsSectionCode').value.trim(),
                rate: parseFloat(document.getElementById('tdsRate').value),
                description: document.getElementById('tdsDescription').value.trim() || null
            };
            const res = await authFetch('/api/settings/taxes/tds', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                closeModal('addTDSModal');
                document.getElementById('createTDSForm').reset();
                fetchTaxMasters();
                initApp();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to add TDS section');
            }
        }

        async function deleteTDS(id) {
            if (!confirm('Are you sure you want to deactivate this TDS section?')) return;
            const res = await authFetch(`/api/settings/taxes/tds/${id}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                fetchTaxMasters();
                initApp();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to deactivate TDS section');
            }
        }

        async function fetchFYs() {
            const res = await authFetch('/api/settings/financial-years');
            if (!res.ok) return;
            cachedFYs = await res.json() || [];
            const tb = document.getElementById('fyTableBody');
            if (!cachedFYs.length) {
                tb.innerHTML = `<tr><td colspan="4" class="px-4 py-4 text-center text-slate-400">No financial years configured. Click + Add FY or Seed FYs.</td></tr>`;
                return;
            }
            tb.innerHTML = cachedFYs.map((f, idx) => `
                <tr class="hover:bg-slate-50 transition">
                    <td class="px-4 py-2.5 font-bold text-slate-800">${escapeHtml(f.code)}</td>
                    <td class="px-4 py-2.5 text-slate-600">${escapeHtml(f.start_date)}</td>
                    <td class="px-4 py-2.5 text-slate-600">${escapeHtml(f.end_date)}</td>
                    <td class="px-4 py-2.5 text-center">
                        <button onclick="deleteFYByIndex(${idx})" class="text-rose-600 hover:underline">Delete</button>
                    </td>
                </tr>
            `).join('');
        }

        function deleteFYByIndex(idx) {
            const f = cachedFYs[idx];
            if (f) deleteFY(f.code);
        }

        async function seedStandardFYs() {
            const res = await authFetch('/api/settings/financial-years/seed', {
                method: 'POST'
            });
            if (res.ok) {
                fetchFYs();
                initApp();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to seed financial years');
            }
        }

        async function submitCreateFY(e) {
            e.preventDefault();
            const payload = {
                code: document.getElementById('newFYCode').value.trim(),
                start_date: document.getElementById('newFYStart').value,
                end_date: document.getElementById('newFYEnd').value
            };
            const res = await authFetch('/api/settings/financial-years', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                closeModal('addFYModal');
                document.getElementById('createFYForm').reset();
                fetchFYs();
                initApp();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to add financial year');
            }
        }

        async function deleteFY(code) {
            if (!confirm(`Delete financial year '${code}'?`)) return;
            const res = await authFetch(`/api/settings/financial-years/${encodeURIComponent(code)}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                fetchFYs();
                initApp();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Could not delete financial year');
            }
        }



        
        // --- Sales Quota / Target Management ---
        async function openTargetModal(userId) {
            const u = cachedUsers.find(item => item.id === userId);
            const userName = u ? (u.full_name || u.username) : `User #${userId}`;
            document.getElementById('targetUserId').value = userId;
            document.getElementById('targetModalSubtitle').textContent = `Set financial targets for ${userName}`;
            
            // Populate FY options in target modal
            const fySelect = document.getElementById('targetFY');
            fySelect.innerHTML = '';
            const res = await authFetch('/api/settings/financial-years');
            if (!res.ok) return;
            const fys = await res.json();
            fys.forEach(f => {
                fySelect.innerHTML += `<option value="${escapeHtml(f.code)}">${escapeHtml(f.code)}</option>`;
            });

            await loadUserTargetsForFY();
            openModal('targetModal');
        }

        async function loadUserTargetsForFY() {
            const userId = document.getElementById('targetUserId').value;
            const fy = document.getElementById('targetFY').value;

            const res = await authFetch(`/api/users/${userId}/targets?fy=${encodeURIComponent(fy)}`);
            if (!res.ok) return;
            const data = await res.json();
            const t = data.targets || {};

            document.getElementById('targetYearly').value = t.yearly_target || '';
            document.getElementById('targetHalfYearly').value = t.half_yearly_target || '';
            document.getElementById('targetQuarterly').value = t.quarterly_target || '';
            document.getElementById('targetMonthly').value = t.monthly_target || '';
            document.getElementById('targetMonthlyBL').value = t.monthly_bottom_line || '';
            document.getElementById('targetYearlyBL').value = t.yearly_bottom_line || '';
        }

        function autoBreakdownTargets() {
            const y = parseFloat(document.getElementById('targetYearly').value) || 0;
            document.getElementById('targetHalfYearly').value = (y / 2).toFixed(2);
            document.getElementById('targetQuarterly').value = (y / 4).toFixed(2);
            document.getElementById('targetMonthly').value = (y / 12).toFixed(2);
        }

        async function submitSaveTargets(e) {
            e.preventDefault();
            const userId = document.getElementById('targetUserId').value;

            const payload = {
                fy_code: document.getElementById('targetFY').value,
                yearly_target: parseFloat(document.getElementById('targetYearly').value) || 0,
                half_yearly_target: parseFloat(document.getElementById('targetHalfYearly').value) || 0,
                quarterly_target: parseFloat(document.getElementById('targetQuarterly').value) || 0,
                monthly_target: parseFloat(document.getElementById('targetMonthly').value) || 0,
                monthly_bottom_line: parseFloat(document.getElementById('targetMonthlyBL').value) || 0,
                yearly_bottom_line: parseFloat(document.getElementById('targetYearlyBL').value) || 0
            };

            const res = await authFetch(`/api/users/${userId}/targets`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                alert('Sales targets saved successfully!');
                closeModal('targetModal');
                loadDashboard();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Failed to save targets');
            }
        }

        window.onload = checkAuth;
