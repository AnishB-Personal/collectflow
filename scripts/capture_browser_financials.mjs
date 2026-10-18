import { chromium } from "playwright";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_URL = process.env.FRONTEND_URL || "http://127.0.0.1:3000";

function parseMoney(text) {
  if (!text) return "0.00";
  const match = text.match(/[-−]?₹?\s*([\d,]+(?:\.\d+)?)/);
  if (!match) return "0.00";
  const isNegative = text.includes("-") || text.includes("−");
  const numStr = match[1].replace(/,/g, "");
  const num = Number(numStr);
  const formatted = num.toFixed(2);
  return isNegative ? `-${formatted}` : formatted;
}

export async function captureBrowserFinancials(options = {}) {
  const runId = options.runId || process.env.CAPTURE_RUN_ID || `run_${Date.now()}_${Math.random().toString(36).substring(2, 8)}`;
  const outputPath = options.outputPath || process.env.CAPTURE_OUTPUT_PATH;

  console.log(`[Browser Financial Capture] Run ID: ${runId}`);
  console.log(`[Browser Financial Capture] Launching Chromium against ${FRONTEND_URL}...`);
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
    env: {
      ...process.env,
      LD_LIBRARY_PATH: `/home/anishb/.local/lib:${process.env.LD_LIBRARY_PATH || ""}`,
    },
  });

  const page = await browser.newPage();
  try {
    // 1. Login
    await page.goto(`${FRONTEND_URL}/login`);
    await page.getByLabel(/Username/i).fill("sandbox_admin");
    await page.getByLabel(/Password/i).fill("AdminPass123!");
    await page.getByRole("button", { name: /Sign In/i }).click();
    await page.waitForURL((url) => !url.pathname.includes("/login"), { timeout: 10000 });

    // 2. Client-side navigation to Reports Hub (preserves in-memory token)
    const mainNav = page.getByRole("navigation", { name: "Main navigation" });
    await mainNav.getByRole("button", { name: "Reports" }).click();
    await page.waitForURL(new RegExp("/reports"), { timeout: 10000 });

    // Wait for live summary to populate KPI cards (wait until text updates from placeholder ₹0)
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="kpi-invoiced-gross"] strong');
      return el && el.textContent && !el.textContent.trim().startsWith("₹0");
    }, { timeout: 15000 });

    // 3. Extract Invoices & Receivables KPIs
    const invoicedGrossText = await page.getByTestId("kpi-invoiced-gross").locator("strong").innerText();
    const outstandingText = await page.getByTestId("kpi-outstanding-receivables").locator("strong").innerText();
    const totalSettledText = await page.getByTestId("kpi-total-settled").locator("strong").innerText();

    // 4. Extract Payments KPIs
    await page.getByTestId("tab-payments").click();
    await page.waitForTimeout(500);
    const cashText = await page.getByTestId("kpi-cash-collected").locator("strong").innerText();
    const tdsText = await page.getByTestId("kpi-tds-deducted").locator("strong").innerText();

    // 5. Extract Executive Attainment KPIs (Exclusive Turnover & Target)
    await page.getByTestId("tab-executives").click();
    await page.waitForTimeout(500);
    const exclusiveText = await page.getByTestId("kpi-actual-invoiced-sales").locator("strong").innerText();
    const targetText = await page.getByTestId("kpi-annual-quota").locator("strong").innerText();

    const invoiced_gross = parseMoney(invoicedGrossText);
    const exclusive_amount = parseMoney(exclusiveText);
    const cash_amount = parseMoney(cashText);
    const tds_amount = parseMoney(tdsText);
    const total_settled = parseMoney(totalSettledText);
    const total_outstanding = parseMoney(outstandingText);
    const annual_target = parseMoney(targetText);

    // Strictly DO NOT derive or hardcode metrics not rendered in the UI
    const result = {
      run_id: runId,
      captured_at: new Date().toISOString(),
      source: "Playwright Headless Chromium DOM",
      frontend_url: FRONTEND_URL,
      invoiced_gross,
      exclusive_amount,
      tax_amount: null, // NOT APPLICABLE in UI
      cash_amount,
      tds_amount,
      total_settled,
      total_outstanding,
      unbilled_po_gross: null, // NOT APPLICABLE in UI
      unbilled_recognized_ar: null, // NOT APPLICABLE in UI
      annual_target,
      raw_dom_values: {
        invoiced_gross_kpi: invoicedGrossText,
        outstanding_kpi: outstandingText,
        total_settled_kpi: totalSettledText,
        cash_kpi: cashText,
        tds_kpi: tdsText,
        exclusive_kpi: exclusiveText,
        annual_target_kpi: targetText,
      },
    };

    const targetPaths = [];
    if (outputPath) {
      targetPaths.push(outputPath);
    } else {
      targetPaths.push(
        path.join(__dirname, "..", "artifacts", "browser_rendered_financials.json"),
        path.join(__dirname, "..", "..", "collectflow", "artifacts", "browser_rendered_financials.json")
      );
    }

    for (const p of targetPaths) {
      fs.mkdirSync(path.dirname(p), { recursive: true });
      fs.writeFileSync(p, JSON.stringify(result, null, 2));
      console.log(`[Browser Financial Capture] Saved snapshot to ${p}`);
    }

    console.log(JSON.stringify(result, null, 2));
    return result;
  } finally {
    await browser.close();
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let outputPath = null;
  let runId = null;
  for (let i = 2; i < process.argv.length; i++) {
    if (process.argv[i] === "--output" && process.argv[i + 1]) {
      outputPath = process.argv[i + 1];
      i++;
    } else if (process.argv[i] === "--run-id" && process.argv[i + 1]) {
      runId = process.argv[i + 1];
      i++;
    }
  }
  captureBrowserFinancials({ outputPath, runId }).catch((err) => {
    console.error("Error capturing browser financials:", err);
    process.exit(1);
  });
}
