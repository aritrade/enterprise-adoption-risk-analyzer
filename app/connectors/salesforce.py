"""
Salesforce connector — pulls support cases, escalation history,
and account metadata using the Salesforce CLI's stored OAuth token.

Authentication is handled by the SF CLI (sf), which stores tokens
obtained via Okta SSO. This connector reads the token at startup
and refreshes it automatically when it expires.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import pathlib
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:  # live-only dependency — not installed/used in the public demo build
    from simple_salesforce import Salesforce
except ImportError:  # pragma: no cover
    Salesforce = None  # type: ignore[assignment]

from app.config import settings
from app.engine.region_map import country_to_region

logger = logging.getLogger(__name__)

SF_CLI_TIMEOUT_SECONDS = 60


def _neutralize_sf_logs() -> None:
    """
    Make the SF CLI's daily log file openable from any process context.

    The modern ``@salesforce/cli`` always writes a daily log file at
    ``~/.sf/sf-YYYY-MM-DD.log`` with no supported flag to disable it
    (``SF_DISABLE_LOG_FILE`` is an old ``sfdx`` env var the current
    ``sf`` ignores). On macOS, that file inherits a
    ``com.apple.provenance`` xattr from whichever process first created
    it (Terminal, an IDE, …). When ``sf`` is later spawned from a
    different security context — uvicorn launched by Cursor, the
    background scheduler — the kernel denies ``open(O_APPEND)`` with
    EPERM. The CLI dies before any output is produced and the connector
    silently falls back to demo data.

    Two-step neutralisation:

    1. ``xattr -c`` strips the provenance flag where possible (works on
       files we own with no SIP-protected ancestors).
    2. We then **delete today's log file** outright so the next ``sf``
       invocation creates a fresh one in its own provenance context,
       which it always has permission to append to. Deletion is safe —
       ``sf`` regenerates the log on demand and we never read it.

    Both steps swallow errors; the function is best-effort.
    """
    sf_dir = pathlib.Path.home() / ".sf"
    if not sf_dir.is_dir():
        return
    log_files = glob.glob(str(sf_dir / "sf-*.log"))
    if not log_files:
        return
    try:
        subprocess.run(
            ["xattr", "-c", *log_files],
            capture_output=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    today = datetime.now().strftime("sf-%Y-%m-%d.log")
    today_log = sf_dir / today
    if today_log.exists():
        try:
            today_log.unlink()
        except OSError as exc:
            logger.debug("Could not delete %s (non-fatal): %s", today_log, exc)


# Backwards-compat alias — older code paths and tests reference this name.
_clear_sf_log_xattrs = _neutralize_sf_logs


def _sf_subprocess_env() -> dict[str, str]:
    """
    Environment for ``sf`` CLI subprocess calls.

    We keep this minimal: ``SF_LOG_LEVEL`` etc. trigger logger code paths
    inside ``@salesforce/core`` that crash with
    ``UnexpectedValueTypeError`` when paired with non-default settings.
    The log file itself is taken care of by ``_neutralize_sf_logs()``.
    """
    env = os.environ.copy()
    env.setdefault("SF_AUTOUPDATE_DISABLE", "true")
    env.setdefault("SF_DISABLE_TELEMETRY", "true")
    return env

CASE_FIELDS = [
    "Id", "CaseNumber", "AccountId", "Account.Name", "Subject",
    "Priority", "Status", "IsClosed", "Origin", "Type",
    "IsEscalated", "CreatedDate", "ClosedDate",
    "ContactId", "Contact.Name", "Contact.Email",
    "OwnerId", "Owner.Name",
]


def _get_sf_cli_token(username: str) -> dict[str, str]:
    """Extract access token and instance URL from the Salesforce CLI."""
    _neutralize_sf_logs()
    try:
        result = subprocess.run(
            ["sf", "org", "display", "--target-org", username, "--json"],
            capture_output=True,
            text=True,
            timeout=SF_CLI_TIMEOUT_SECONDS,
            env=_sf_subprocess_env(),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "EPERM" in stderr and ".sf/sf-" in stderr:
                raise RuntimeError(
                    "sf CLI hit a macOS provenance EPERM on its log file. "
                    "The connector deleted today's log on this attempt; if "
                    "you still see this, run `rm -f ~/.sf/sf-*.log` from a "
                    "terminal owned by the same user as this process, then "
                    "POST /api/sync/run/salesforce to retry."
                )
            raise RuntimeError(f"sf org display failed: {stderr}")
        data = json.loads(result.stdout)
        org = data.get("result", {})
        token = org.get("accessToken")
        instance = org.get("instanceUrl")
        if not token or not instance:
            raise RuntimeError(
                "SF CLI returned no accessToken/instanceUrl — "
                "run `sf org login web` to re-authenticate"
            )
        return {"access_token": token, "instance_url": instance}
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"sf org display timed out after {SF_CLI_TIMEOUT_SECONDS}s — "
            "the Salesforce API may be slow or the CLI may be stuck. "
            "Try `sf org display --target-org "
            f"{username}` from a terminal to verify."
        ) from exc
    except FileNotFoundError:
        raise RuntimeError(
            "Salesforce CLI (sf) not found on PATH. "
            "Install it: https://developer.salesforce.com/tools/salesforcecli"
        )


class SalesforceConnector:
    def __init__(self) -> None:
        self._sf: Optional[Salesforce] = None

    def _connect(self) -> Salesforce:
        if self._sf is not None:
            return self._sf
        creds = _get_sf_cli_token(settings.sf_username)
        self._sf = Salesforce(
            session_id=creds["access_token"],
            instance_url=creds["instance_url"],
        )
        logger.info("Connected to Salesforce via CLI token (%s)", creds["instance_url"])
        return self._sf

    def _reconnect(self) -> Salesforce:
        """Force a fresh token from the CLI (handles expired sessions)."""
        self._sf = None
        return self._connect()

    def _query(self, soql: str) -> list[dict[str, Any]]:
        try:
            sf = self._connect()
            result = sf.query_all(soql)
        except Exception as exc:
            if "INVALID_SESSION_ID" in str(exc) or "Session expired" in str(exc):
                logger.warning("SF session expired, refreshing token via CLI")
                sf = self._reconnect()
                result = sf.query_all(soql)
            else:
                raise
        records: list[dict[str, Any]] = result.get("records", [])
        for rec in records:
            rec.pop("attributes", None)
            for v in rec.values():
                if isinstance(v, dict):
                    v.pop("attributes", None)
        return records

    def get_open_cases(self, days_back: int = 90) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        fields = ", ".join(CASE_FIELDS)
        soql = (
            f"SELECT {fields} FROM Case "
            f"WHERE IsClosed = false AND CreatedDate >= {cutoff} "
            "ORDER BY Priority ASC, CreatedDate DESC"
        )
        return self._query(soql)

    def get_escalated_cases(self, days_back: int = 180) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        fields = ", ".join(CASE_FIELDS)
        soql = (
            f"SELECT {fields} FROM Case "
            f"WHERE IsEscalated = true AND CreatedDate >= {cutoff} "
            "ORDER BY CreatedDate DESC"
        )
        return self._query(soql)

    def get_cases_by_account(self, account_id: str) -> list[dict[str, Any]]:
        fields = ", ".join(CASE_FIELDS)
        soql = (
            f"SELECT {fields} FROM Case "
            f"WHERE AccountId = '{account_id}' "
            "ORDER BY CreatedDate DESC LIMIT 200"
        )
        return self._query(soql)

    def get_case_comments(self, case_id: str) -> list[dict[str, Any]]:
        soql = (
            "SELECT Id, ParentId, CommentBody, CreatedDate, CreatedBy.Name "
            f"FROM CaseComment WHERE ParentId = '{case_id}' "
            "ORDER BY CreatedDate DESC LIMIT 50"
        )
        return self._query(soql)

    def get_account_summary(self, account_id: str) -> dict[str, Any]:
        """Aggregate case stats for one account."""
        cases = self.get_cases_by_account(account_id)
        total = len(cases)
        open_cases = [c for c in cases if not c.get("IsClosed", False)]
        open_count = len(open_cases)
        escalated = sum(1 for c in open_cases if c.get("IsEscalated"))
        p1_count = sum(
            1 for c in open_cases
            if (c.get("Priority") or "").startswith("P1")
        )
        p2_count = sum(
            1 for c in open_cases
            if (c.get("Priority") or "").startswith("P2")
        )
        return {
            "account_id": account_id,
            "total_cases": total,
            "open_cases": open_count,
            "escalated_cases": escalated,
            "p1_cases": p1_count,
            "p2_cases": p2_count,
            "cases": cases,
        }

    def get_license_assets(self, account_id: str) -> list[dict[str, Any]]:
        """Fetch license/entitlement Asset records for an account."""
        soql = (
            "SELECT Id, Name, Product2.Name, Product2.Family, "
            "Quantity, Status, InstallDate, UsageEndDate, "
            "SerialNumber "
            f"FROM Asset WHERE AccountId = '{account_id}' "
            "ORDER BY Product2.Family ASC, InstallDate DESC "
            "LIMIT 500"
        )
        return self._query(soql)

    def get_license_summary(self, account_id: str) -> dict[str, Any]:
        """Build a license summary grouped by Nutanix product category."""
        assets = self.get_license_assets(account_id)
        families: dict[str, dict] = {}
        for a in assets:
            prod = a.get("Product2") or {}
            name = prod.get("Name") or a.get("Name") or "Unknown"
            family = _categorize_product(name, prod.get("Family", ""))
            qty = a.get("Quantity") or 0
            status = a.get("Status") or ""
            end_date = a.get("UsageEndDate")

            if family not in families:
                families[family] = {
                    "family": family,
                    "products": [],
                    "total_quantity": 0,
                    "active_quantity": 0,
                    "expired_quantity": 0,
                }
            fam = families[family]
            fam["total_quantity"] += qty
            is_active = status not in ("Expired", "Obsolete", "Retired")
            if is_active:
                fam["active_quantity"] += qty
            else:
                fam["expired_quantity"] += qty
            fam["products"].append({
                "name": name,
                "quantity": qty,
                "status": status,
                "install_date": a.get("InstallDate"),
                "end_date": end_date,
                "serial": a.get("SerialNumber"),
            })

        return {
            "account_id": account_id,
            "total_assets": len(assets),
            "families": list(families.values()),
        }

    def get_account_contacts(self, account_id: str) -> list[dict[str, Any]]:
        """Fetch contacts for an account, prioritising primary/decision-maker roles."""
        soql = (
            "SELECT Id, Name, Email, Title, Phone, Department, "
            "MailingCity, MailingState, MailingCountry, "
            "IsDirect, CreatedDate "
            f"FROM Contact WHERE AccountId = '{account_id}' "
            "AND Email != null "
            "ORDER BY CreatedDate ASC LIMIT 50"
        )
        contacts = self._query(soql)

        primary_titles = {
            "cto", "cio", "vp", "vice president", "director", "head",
            "chief", "svp", "senior vice president", "manager",
            "it director", "infrastructure", "platform",
        }
        for c in contacts:
            title_lower = (c.get("Title") or "").lower()
            c["is_primary"] = any(t in title_lower for t in primary_titles)

        contacts.sort(key=lambda c: (not c.get("is_primary", False), c.get("CreatedDate", "")))
        return contacts

    def get_renewal_outlook(self, account_id: str) -> dict[str, Any]:
        """
        Fetch renewal Opportunity data for an account: risk score, risk
        category, sentiment notes, nearest renewal date, and contract value.
        Includes per-product line-item breakdown for each renewal.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        soql = (
            "SELECT Id, Name, CloseDate, StageName, "
            "Renewal_Risk_Score__c, Renewal_Risk__c, "
            "Renewal_Risk_Subcategory__c, Renewal_Sentiment__c, "
            "Amount_Total__c "
            "FROM Opportunity "
            f"WHERE AccountId = '{account_id}' "
            "AND Renewal_Pipeline_Type__c != null "
            "AND StageName != '9 - ATR Closed' "
            f"AND CloseDate >= {now} "
            "ORDER BY CloseDate ASC LIMIT 20"
        )
        rows = self._query(soql)
        if not rows:
            return {
                "has_renewal": False,
                "days_to_renewal": None,
                "renewal_date": None,
                "renewal_risk_score": None,
                "renewal_risk_label": "unknown",
                "renewal_risk_subcategory": None,
                "renewal_sentiment_notes": None,
                "contract_value": 0,
                "total_renewal_value": 0,
                "renewal_count": 0,
                "renewals": [],
            }

        opp_ids = [r["Id"] for r in rows]
        product_map = self._get_renewal_product_breakdown(opp_ids)

        nearest = rows[0]
        close_date_str = nearest.get("CloseDate")
        days_to = None
        if close_date_str:
            try:
                dt = datetime.strptime(close_date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                days_to = (dt - datetime.now(timezone.utc)).days
            except (ValueError, TypeError):
                pass

        risk_score_raw = nearest.get("Renewal_Risk_Score__c")
        risk_score = round(risk_score_raw * 100, 1) if risk_score_raw is not None else None
        risk_label = nearest.get("Renewal_Risk__c") or "unknown"

        sentiment_notes = nearest.get("Renewal_Sentiment__c") or None
        if sentiment_notes and sentiment_notes.startswith("Blank update"):
            sentiment_notes = None

        total_value = sum(r.get("Amount_Total__c") or 0 for r in rows)

        renewals = []
        for r in rows:
            cd = r.get("CloseDate")
            rs = r.get("Renewal_Risk_Score__c")
            opp_id = r["Id"]
            renewals.append({
                "id": opp_id,
                "name": r.get("Name", ""),
                "close_date": cd,
                "stage": r.get("StageName", ""),
                "risk_score": round(rs * 100, 1) if rs is not None else None,
                "risk_label": r.get("Renewal_Risk__c") or "unknown",
                "value": r.get("Amount_Total__c") or 0,
                "products": product_map.get(opp_id, []),
            })

        return {
            "has_renewal": True,
            "days_to_renewal": days_to,
            "renewal_date": close_date_str,
            "renewal_risk_score": risk_score,
            "renewal_risk_label": risk_label,
            "renewal_risk_subcategory": nearest.get("Renewal_Risk_Subcategory__c"),
            "renewal_sentiment_notes": sentiment_notes,
            "contract_value": nearest.get("Amount_Total__c") or 0,
            "total_renewal_value": round(total_value, 2),
            "renewal_count": len(rows),
            "renewals": renewals,
        }

    def _get_renewal_product_breakdown(
        self, opp_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch OpportunityLineItem records and group by product family."""
        if not opp_ids:
            return {}
        id_list = "','".join(opp_ids)
        soql = (
            "SELECT OpportunityId, Product2.Name, Product2.Family, "
            "Quantity, UnitPrice "
            "FROM OpportunityLineItem "
            f"WHERE OpportunityId IN ('{id_list}') "
            "ORDER BY OpportunityId"
        )
        try:
            items = self._query(soql)
        except Exception:
            return {}

        opp_buckets: dict[str, dict[str, dict]] = {}
        for item in items:
            opp_id = item.get("OpportunityId", "")
            prod_name = (item.get("Product2") or {}).get("Name", "")
            prod_family = (item.get("Product2") or {}).get("Family", "")
            qty = item.get("Quantity") or 0
            price = item.get("UnitPrice") or 0

            if prod_name.startswith("R-IA-"):
                continue

            ncp_family = _classify_renewal_product(prod_name, prod_family)

            if opp_id not in opp_buckets:
                opp_buckets[opp_id] = {}
            bucket = opp_buckets[opp_id]
            if ncp_family not in bucket:
                bucket[ncp_family] = {"product": ncp_family, "qty": 0, "value": 0}
            bucket[ncp_family]["qty"] += qty
            bucket[ncp_family]["value"] += qty * price

        result: dict[str, list[dict]] = {}
        for opp_id, fams in opp_buckets.items():
            sorted_fams = sorted(
                fams.values(), key=lambda f: f["value"], reverse=True
            )
            for f in sorted_fams:
                f["value"] = round(f["value"], 2)
            result[opp_id] = sorted_fams
        return result

    def get_account_owner_email(self, account_id: str) -> dict[str, Any]:
        """Fetch the account owner details (the internal Nutanix rep)."""
        soql = (
            "SELECT OwnerId, Owner.Name, Owner.Email "
            f"FROM Account WHERE Id = '{account_id}' LIMIT 1"
        )
        rows = self._query(soql)
        if rows:
            owner = rows[0].get("Owner") or {}
            return {"name": owner.get("Name", ""), "email": owner.get("Email", "")}
        return {"name": "", "email": ""}

    def get_account_financials(self, account_id: str) -> dict[str, Any]:
        """Fetch financial data from Account fields + computed from Opportunities."""
        acct_soql = (
            "SELECT AnnualRevenue, NumberOfEmployees, "
            "D_B_Number_of_Employees__c, "
            "Imputed_Number_of_Employees__c, "
            "Total_ISV_Bookings_New_Existing_Only__c, "
            "CQ_Bookings__c, "
            "Open_Renewal_ACV_Pipeline__c, "
            "Average_Deal_Size_Bookings__c, "
            "Account_Segment__c, "
            "Contract_Start_Date__c, "
            "Most_Recent_Sales_Date__c "
            f"FROM Account WHERE Id = '{account_id}' LIMIT 1"
        )
        acct_rows = self._query(acct_soql)
        r = acct_rows[0] if acct_rows else {}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d")
        opp_soql = (
            "SELECT "
            "SUM(Amount_Total__c) lifetime_acv, "
            "COUNT(Id) total_opps "
            "FROM Opportunity "
            f"WHERE AccountId = '{account_id}' "
            "AND StageName LIKE '%Closed Won%'"
        )
        opp_rows = self._query(opp_soql)
        lifetime_bookings = (opp_rows[0].get("lifetime_acv") or 0) if opp_rows else 0
        total_opps = (opp_rows[0].get("total_opps") or 0) if opp_rows else 0

        arr_soql = (
            "SELECT SUM(Amount_Total__c) active_arr "
            "FROM Opportunity "
            f"WHERE AccountId = '{account_id}' "
            "AND StageName LIKE '%Closed Won%' "
            "AND Renewal_Pipeline_Type__c != null "
            f"AND CloseDate >= {cutoff}"
        )
        arr_rows = self._query(arr_soql)
        computed_arr = (arr_rows[0].get("active_arr") or 0) if arr_rows else 0

        return {
            "account_id": account_id,
            "annual_revenue": r.get("AnnualRevenue"),
            "number_of_employees": r.get("NumberOfEmployees"),
            "dnb_employees": r.get("D_B_Number_of_Employees__c"),
            "imputed_employees": r.get("Imputed_Number_of_Employees__c"),
            "arr": computed_arr if computed_arr else None,
            "total_lifetime_bookings": round(lifetime_bookings, 2) if lifetime_bookings else None,
            "total_closed_won_opps": total_opps,
            "tcv_bookings": r.get("Total_ISV_Bookings_New_Existing_Only__c"),
            "open_pipeline": r.get("CQ_Bookings__c"),
            "open_renewal_pipeline": r.get("Open_Renewal_ACV_Pipeline__c"),
            "avg_deal_size": r.get("Average_Deal_Size_Bookings__c"),
            "account_segment": r.get("Account_Segment__c"),
            "contract_start_date": r.get("Contract_Start_Date__c"),
            "last_transaction_date": r.get("Most_Recent_Sales_Date__c"),
        }

    def search_accounts(self, term: str) -> list[dict[str, Any]]:
        safe_term = term.replace("'", "\\'")
        soql = (
            "SELECT Id, Name, Industry, Type, OwnerId, Owner.Name "
            f"FROM Account WHERE Name LIKE '%{safe_term}%' "
            "ORDER BY Name LIMIT 25"
        )
        return self._query(soql)

    def get_all_customer_accounts(self) -> list[dict[str, Any]]:
        """Fetch all Customer/Prospect accounts with lightweight fields for the in-memory index."""
        soql = (
            "SELECT Id, Name, Type, Industry, Owner.Name, "
            "CXM_Owner__r.Name, CXM_Owner__r.Email, "
            "Install_Base_400_Program_Classification__c, "
            "BillingCountry "
            "FROM Account "
            "WHERE Type IN ('Customer', 'Prospect') "
            "ORDER BY Name"
        )
        rows = self._query(soql)
        result = []
        for r in rows:
            owner = r.get("Owner") or {}
            cxm = r.get("CXM_Owner__r") or {}
            billing_country = r.get("BillingCountry") or ""
            result.append({
                "id": r["Id"],
                "name": r["Name"],
                "type": r.get("Type", ""),
                "industry": r.get("Industry", ""),
                "owner": owner.get("Name", ""),
                "cxm": cxm.get("Name", ""),
                "cxm_email": cxm.get("Email", ""),
                "cxm_program": r.get("Install_Base_400_Program_Classification__c") or "",
                "name_lower": r["Name"].lower(),
                "billing_country": billing_country,
                "region": country_to_region(billing_country),
            })
        return result

    def get_license_entitlements(self, account_id: str) -> dict[str, Any]:
        """
        Query the License__c object for an account's real license entitlements.
        Returns Used / Total / Metric per product-tier, matching the portal's
        Licensing overview page.
        """
        soql = (
            "SELECT License_SKU__c, LicenseType__c, Quantity__c, "
            "CCUs_Used__c, Available_quantity__c, License_Status__c "
            f"FROM License__c WHERE Account_ID__c = '{account_id}' "
            "AND isExpired__c = false"
        )
        records = self._query(soql)

        buckets: dict[str, dict[str, Any]] = {}
        for rec in records:
            sku = rec.get("License_SKU__c") or ""
            qty = rec.get("Quantity__c") or 0
            used = rec.get("CCUs_Used__c") or 0
            avail_str = rec.get("Available_quantity__c") or ""

            metric = ""
            if ":" in avail_str:
                metric = avail_str.split(":")[1].strip().lower()

            product, tier = _classify_license_sku(sku, rec.get("LicenseType__c", ""))
            if not tier:
                lt = (rec.get("LicenseType__c") or "").lower()
                if "ultimate" in lt:
                    tier = "Ultimate"
                elif "pro" in lt:
                    tier = "Pro"
                elif "starter" in lt:
                    tier = "Starter"
            key = f"{product}|{tier}"

            if key not in buckets:
                buckets[key] = {
                    "product": product,
                    "tier": tier,
                    "total": 0,
                    "used": 0,
                    "metric": metric or "cores",
                }
            buckets[key]["total"] += qty
            buckets[key]["used"] += used
            if metric:
                buckets[key]["metric"] = metric

        products = sorted(buckets.values(), key=lambda b: (b["product"], b["tier"]))
        for p in products:
            p["available"] = max(0, p["total"] - p["used"])
            p["adoption_pct"] = round(p["used"] / p["total"] * 100) if p["total"] > 0 else 0

        return {
            "account_id": account_id,
            "total_licenses": len(records),
            "products": products,
        }


    # ── Internal engagement (replaces Glean) ──────────────────────────

    def get_internal_engagement(self, account_id: str) -> dict[str, Any]:
        """
        Pull internal engagement signals from SF objects: Task, Event,
        and internal (non-published) CaseComments.  Returns the same
        shape consumed by the risk scorer / aggregator that previously
        came from the Glean connector.
        """
        tasks = self._query(
            "SELECT Id, Subject, CreatedDate, Status, TaskSubtype, "
            "OwnerId, Owner.Name "
            f"FROM Task WHERE AccountId = '{account_id}' "
            "AND CreatedDate >= LAST_N_DAYS:180 "
            "ORDER BY CreatedDate DESC LIMIT 200"
        )
        events = self._query(
            "SELECT Id, Subject, StartDateTime, EndDateTime, "
            "OwnerId, Owner.Name "
            f"FROM Event WHERE AccountId = '{account_id}' "
            "AND CreatedDate >= LAST_N_DAYS:180 "
            "ORDER BY StartDateTime DESC LIMIT 200"
        )
        internal_comments = self._query(
            "SELECT Id, ParentId, CommentBody, CreatedDate, "
            "CreatedBy.Name, IsPublished "
            "FROM CaseComment "
            f"WHERE ParentId IN (SELECT Id FROM Case WHERE AccountId = '{account_id}') "
            "AND IsPublished = false "
            "AND CreatedDate >= LAST_N_DAYS:180 "
            "ORDER BY CreatedDate DESC LIMIT 200"
        )
        escalated_cases = self._query(
            "SELECT Id, CaseNumber, Subject, Priority, IsEscalated, "
            "CreatedDate "
            f"FROM Case WHERE AccountId = '{account_id}' "
            "AND CreatedDate >= LAST_N_DAYS:180 "
            "AND (IsEscalated = true "
            "     OR Priority IN ('P1 - Critical', 'P2 - Critical')) "
            "ORDER BY CreatedDate DESC LIMIT 50"
        )

        now = datetime.now(timezone.utc)
        cutoff_30 = now - timedelta(days=30)
        cutoff_60 = now - timedelta(days=60)

        def _parse_dt(s: str | None) -> datetime | None:
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None

        task_count = len(tasks)
        event_count = len(events)
        comment_count = len(internal_comments)
        total = task_count + event_count + comment_count

        recent_30 = 0
        prev_30_60 = 0
        all_items: list[dict[str, Any]] = []

        for t in tasks:
            dt = _parse_dt(t.get("CreatedDate"))
            if dt and dt >= cutoff_30:
                recent_30 += 1
            elif dt and dt >= cutoff_60:
                prev_30_60 += 1
            owner = t.get("Owner") or {}
            all_items.append({
                "title": t.get("Subject") or "Task",
                "snippet": f"Task — {t.get('Status', '')}",
                "source": "Tasks",
                "author": owner.get("Name", ""),
                "date": t.get("CreatedDate", ""),
                "url": "",
                "type": "task",
            })

        for e in events:
            dt = _parse_dt(e.get("StartDateTime"))
            if dt and dt >= cutoff_30:
                recent_30 += 1
            elif dt and dt >= cutoff_60:
                prev_30_60 += 1
            owner = e.get("Owner") or {}
            all_items.append({
                "title": e.get("Subject") or "Meeting",
                "snippet": f"Meeting — {e.get('StartDateTime', '')[:10]}",
                "source": "Meetings",
                "author": owner.get("Name", ""),
                "date": e.get("StartDateTime", ""),
                "url": "",
                "type": "event",
            })

        for c in internal_comments:
            dt = _parse_dt(c.get("CreatedDate"))
            if dt and dt >= cutoff_30:
                recent_30 += 1
            elif dt and dt >= cutoff_60:
                prev_30_60 += 1
            created_by = c.get("CreatedBy") or {}
            body = c.get("CommentBody") or ""
            all_items.append({
                "title": f"Internal note on case",
                "snippet": body[:200],
                "source": "Internal Comments",
                "author": created_by.get("Name", ""),
                "date": c.get("CreatedDate", ""),
                "url": "",
                "type": "comment",
            })

        escalation_count = len(escalated_cases)

        if total == 0:
            trend = "none"
        elif recent_30 > prev_30_60 * 1.3:
            trend = "increasing"
        elif recent_30 < prev_30_60 * 0.7 and prev_30_60 > 0:
            trend = "declining"
        else:
            trend = "stable"

        source_counts: dict[str, int] = {}
        for item in all_items:
            src = item["source"]
            source_counts[src] = source_counts.get(src, 0) + 1
        top_sources = [
            {"source": s, "count": c}
            for s, c in sorted(source_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        unique_owners = {
            item["author"] for item in all_items if item.get("author")
        }

        latest_date: str | None = None
        for item in all_items:
            d = item.get("date", "")
            if d and (not latest_date or d > latest_date):
                latest_date = d

        signals: list[str] = []
        if total == 0:
            signals.append("No internal activity found — potential blind spot")
        elif total < 5:
            signals.append(f"Only {total} internal touchpoints — low engagement")
        if recent_30 == 0 and total > 5:
            signals.append("No activity in the last 30 days — engagement may have stalled")
        if trend == "declining":
            signals.append("Internal activity trend is declining")
        if escalation_count > 0:
            signals.append(
                f"{escalation_count} escalated/P1-P2 case(s) in the last 180 days"
            )
        if comment_count == 0 and total > 5:
            signals.append("No internal case comments — documentation gap")

        all_items.sort(key=lambda x: x.get("date", ""), reverse=True)

        return {
            "account_id": account_id,
            "source": "salesforce_engagement",
            "total_mentions": total,
            "recent_mentions_30d": recent_30,
            "escalation_mentions": escalation_count,
            "knowledge_articles": comment_count,
            "top_sources": top_sources,
            "last_mentioned": latest_date,
            "mention_trend": trend,
            "top_results": all_items[:10],
            "glean_risk_signals": signals,
            "unique_contributors": len(unique_owners),
            "task_count": task_count,
            "event_count": event_count,
            "comment_count": comment_count,
        }

    # ── Infrastructure: Cluster__c ────────────────────────────────────

    _CLUSTER_FIELDS = [
        "Id", "Name", "Cluster_Name__c", "Cluster_ID__c",
        "Number_of_Nodes__c", "Nutanix_core_version__c",
        "Hypervisor_Type__c", "Hypervisor_version__c",
        "NCC_Version_Pulse__c", "PC_Version_Pulse__c",
        "LCM_Version_Pulse__c", "Foundation_Version_Pulse__c",
        "AFS_Version_Pulse__c", "Buckets_version_Pulse__c",
        "Calm_version_Pulse__c", "Era_Version_Pulse__c",
        "Karbon_version_Pulse__c", "File_Analytics_Version_Pulse__c",
        "Last_Pulse__c", "Pulse_Declined__c",
        "Contract_Status__c", "Cluster_Status__c",
        "Number_of_CBL_Nodes__c", "Support_End_Date__c",
        "Support_Level__c", "HW_Partner__c",
        "Is_POC_Cluster__c", "Cloud_Provider__c",
        "Account__c",
    ]

    def get_account_clusters(self, account_id: str) -> list[dict[str, Any]]:
        """Query Cluster__c records for a customer account.

        Returns a list of dicts with cluster details including node count,
        AOS version, pulse status, etc.  This is the authoritative source
        for infrastructure data — much more accurate than the portal's
        /accountportalvalues endpoint.
        """
        conn = self._connect()
        fields = ", ".join(self._CLUSTER_FIELDS)
        soql = (
            f"SELECT {fields} FROM Cluster__c "
            f"WHERE Account__c = '{account_id}' "
            f"ORDER BY Number_of_Nodes__c DESC NULLS LAST"
        )
        result = conn.query_all(soql)
        records = result.get("records", [])
        for r in records:
            r.pop("attributes", None)
        logger.info(
            "Cluster__c for %s: %d clusters, %d total nodes",
            account_id,
            len(records),
            sum(r.get("Number_of_Nodes__c") or 0 for r in records),
        )
        return records

    def get_infrastructure_summary(self, account_id: str) -> dict[str, Any]:
        """
        Build a comprehensive infrastructure summary from Cluster__c data.

        Returns cluster counts, node counts, version distributions for
        AOS / PC / NCC / hypervisor, AHV vs ESXi split, hardware partner
        breakdown, contract status, and pulse health.
        """
        clusters = self.get_account_clusters(account_id)

        _EXCLUDE_STATUSES = frozenset({
            "terminated", "unlicensed", "never licensed", "hibernated",
        })

        active: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        def _inc(d: dict, key: str, nodes: int = 0) -> None:
            if not key:
                return
            if key not in d:
                d[key] = {"clusters": 0, "nodes": 0}
            d[key]["clusters"] += 1
            d[key]["nodes"] += nodes

        aos_map: dict[str, dict] = {}
        pc_map: dict[str, dict] = {}
        ncc_map: dict[str, dict] = {}
        hv_type_map: dict[str, dict] = {}
        ahv_ver_map: dict[str, dict] = {}
        esxi_ver_map: dict[str, dict] = {}
        hw_map: dict[str, dict] = {}
        contract_map: dict[str, dict] = {}
        lcm_map: dict[str, dict] = {}
        foundation_map: dict[str, dict] = {}
        files_map: dict[str, dict] = {}

        total_nodes = 0
        pulse_active = 0
        pulse_stale = 0
        pulse_disabled = 0

        for rec in clusters:
            nodes = int(rec.get("Number_of_Nodes__c") or 0)
            if nodes == 0:
                continue
            status = (rec.get("Cluster_Status__c") or "").lower()
            if status in _EXCLUDE_STATUSES:
                excluded.append(rec)
                continue

            active.append(rec)
            total_nodes += nodes

            aos = rec.get("Nutanix_core_version__c") or ""
            pc = rec.get("PC_Version_Pulse__c") or ""
            ncc = rec.get("NCC_Version_Pulse__c") or ""
            hv_type = rec.get("Hypervisor_Type__c") or ""
            hv_ver = rec.get("Hypervisor_version__c") or ""
            hw = rec.get("HW_Partner__c") or ""
            contract = rec.get("Contract_Status__c") or "Unknown"
            lcm = rec.get("LCM_Version_Pulse__c") or ""
            foundation = rec.get("Foundation_Version_Pulse__c") or ""
            files = rec.get("AFS_Version_Pulse__c") or ""

            _inc(aos_map, aos, nodes)
            _inc(pc_map, pc, nodes)
            _inc(ncc_map, ncc, nodes)
            _inc(hv_type_map, hv_type, nodes)
            _inc(hw_map, hw, nodes)
            _inc(contract_map, contract, nodes)
            if lcm:
                _inc(lcm_map, lcm, nodes)
            if foundation:
                _inc(foundation_map, foundation, nodes)
            if files:
                _inc(files_map, files, nodes)

            if hv_type.upper() == "AHV" and hv_ver:
                _inc(ahv_ver_map, hv_ver, nodes)
            elif hv_type.upper() == "ESXI" and hv_ver:
                _inc(esxi_ver_map, hv_ver, nodes)

            pulse_declined = bool(rec.get("Pulse_Declined__c"))
            _PULSE_VER_FIELDS = (
                "NCC_Version_Pulse__c", "PC_Version_Pulse__c",
                "LCM_Version_Pulse__c", "Foundation_Version_Pulse__c",
                "AFS_Version_Pulse__c", "Buckets_version_Pulse__c",
            )
            has_pulse_versions = any(rec.get(f) for f in _PULSE_VER_FIELDS)
            if pulse_declined:
                pulse_disabled += 1
            elif has_pulse_versions:
                pulse_active += 1
            else:
                pulse_stale += 1

        def _sort_map(m: dict) -> list[dict]:
            return sorted(
                [{"version": k, **v} for k, v in m.items() if k],
                key=lambda x: x["nodes"], reverse=True,
            )

        _PULSE_VER_FIELDS = (
            "NCC_Version_Pulse__c", "PC_Version_Pulse__c",
            "LCM_Version_Pulse__c", "Foundation_Version_Pulse__c",
            "AFS_Version_Pulse__c", "Buckets_version_Pulse__c",
        )

        cluster_list = []
        for rec in active:
            has_pulse = (
                not bool(rec.get("Pulse_Declined__c"))
                and any(rec.get(f) for f in _PULSE_VER_FIELDS)
            )
            cluster_list.append({
                "cluster_name": rec.get("Cluster_Name__c") or rec.get("Name") or "",
                "cluster_id": rec.get("Cluster_ID__c") or rec.get("Name") or "",
                "node_count": int(rec.get("Number_of_Nodes__c") or 0),
                "aos_version": rec.get("Nutanix_core_version__c") or "",
                "hypervisor_type": rec.get("Hypervisor_Type__c") or "",
                "hypervisor_version": rec.get("Hypervisor_version__c") or "",
                "pc_version": rec.get("PC_Version_Pulse__c") or "",
                "ncc_version": rec.get("NCC_Version_Pulse__c") or "",
                "lcm_version": rec.get("LCM_Version_Pulse__c") or "",
                "foundation_version": rec.get("Foundation_Version_Pulse__c") or "",
                "files_version": rec.get("AFS_Version_Pulse__c") or "",
                "objects_version": rec.get("Buckets_version_Pulse__c") or "",
                "calm_version": rec.get("Calm_version_Pulse__c") or "",
                "era_version": rec.get("Era_Version_Pulse__c") or "",
                "karbon_version": rec.get("Karbon_version_Pulse__c") or "",
                "file_analytics_version": rec.get("File_Analytics_Version_Pulse__c") or "",
                "hw_partner": rec.get("HW_Partner__c") or "",
                "contract_status": rec.get("Contract_Status__c") or "",
                "support_end_date": rec.get("Support_End_Date__c") or "",
                "support_level": rec.get("Support_Level__c") or "",
                "pulse_active": has_pulse,
                "is_poc": bool(rec.get("Is_POC_Cluster__c")),
                "cloud_provider": rec.get("Cloud_Provider__c") or "",
            })

        return {
            "account_id": account_id,
            "total_clusters": len(active),
            "total_nodes": total_nodes,
            "excluded_clusters": len(excluded),
            "aos_versions": _sort_map(aos_map),
            "pc_versions": _sort_map(pc_map),
            "ncc_versions": _sort_map(ncc_map),
            "hypervisor_distribution": _sort_map(hv_type_map),
            "ahv_versions": _sort_map(ahv_ver_map),
            "esxi_versions": _sort_map(esxi_ver_map),
            "lcm_versions": _sort_map(lcm_map),
            "foundation_versions": _sort_map(foundation_map),
            "files_versions": _sort_map(files_map),
            "hardware_partners": _sort_map(hw_map),
            "contract_breakdown": _sort_map(contract_map),
            "pulse_summary": {
                "active": pulse_active,
                "stale": pulse_stale,
                "disabled": pulse_disabled,
                "total": pulse_active + pulse_stale + pulse_disabled,
            },
            "clusters": cluster_list,
        }


_LICENSE_SKU_MAP = [
    # NCI — Nutanix Cloud Infrastructure
    ("SW-NCP-NCI", "NCI", ""),
    ("SW-NCI-D-", "NCI", ""),
    ("SW-NCI-EDGE", "NCI-Edge", ""),
    ("SW-NCI-VDI", "NCI-VDI", ""),
    ("SW-NCI-ULT", "NCI", "Ultimate"),
    ("SW-NCI-PRO", "NCI", "Pro"),
    ("SW-NCI-STR", "NCI", "Starter"),
    ("SW-NCI-", "NCI", ""),
    # NCM — Nutanix Cloud Manager
    ("SW-NCP-NCM", "NCM", ""),
    ("SW-NCM-EDGE", "NCM-Edge", ""),
    ("SW-NCM-ULT", "NCM", "Ultimate"),
    ("SW-NCM-STR", "NCM", "Starter"),
    ("SW-NCM-PRO", "NCM", "Pro"),
    ("SW-NCM-", "NCM", ""),
    # NKP — Nutanix Kubernetes Platform
    ("SW-NKP-ULT", "NKP", "Ultimate"),
    ("SW-NKP-PRO", "NKP", "Pro"),
    ("SW-NKP-STR", "NKP", "Starter"),
    ("SW-NKP-", "NKP", ""),
    # NUS — Nutanix Unified Storage
    ("SW-NUS-ULT", "NUS", "Ultimate"),
    ("SW-NUS-PRO", "NUS", "Pro"),
    ("SW-NUS-STR", "NUS", "Starter"),
    ("SWA-NUS-", "NUS", ""),
    ("SW-NUS-", "NUS", ""),
    # NDB — Nutanix Database Service
    ("SW-NDB-ULT", "NDB", "Ultimate"),
    ("SW-NDB-PRO", "NDB", "Pro"),
    ("SW-NDB-STR", "NDB", "Starter"),
    ("SW-NDB-", "NDB", ""),
    # NAI — Nutanix Enterprise AI
    ("SW-NAI-GPT", "NAI", ""),
    ("SW-NAI-NKP", "NAI", ""),
    ("SW-NAI-", "NAI", ""),
    # NC2, NDK, NDL
    ("SW-NC2", "NC2", ""),
    ("SW-NDK", "NDK", ""),
    ("SW-NDL", "NDL", ""),
    # NUS sub-products (map to NUS family)
    ("SW-FILES-", "NUS", ""),
    ("SW-OBJ-", "NUS", ""),
    # Legacy product names (map to current families)
    ("SW-CALM-", "NCM", ""),
    ("SW-ERA-", "NDB", ""),
    ("SW-FLOW-", "NCI", ""),
    ("SW-SEC-", "NCM", ""),
    ("SW-VDI-", "NCI-VDI", ""),
    ("SW-AOS-", "NCI", ""),
    ("LIC-PRS-", "NCM", ""),
]

_NCP_SOFTWARE_FAMILIES = frozenset({
    "NCI", "NCM", "NKP", "NUS", "NDB", "NAI",
    "NC2", "NDK", "NDL",
    "NCI-VDI", "NCI-Edge", "NCM-Edge",
})


def _classify_license_sku(sku: str, license_type: str = "") -> tuple[str, str]:
    """Map a License_SKU__c value to (product, tier).

    Returns ("_hardware", "") for node/appliance licenses (L-PRO-*, DELL-LIC-*,
    LEN-LIC-*, etc.) and ("_other", "") for unrecognised SKUs that aren't part
    of the Nutanix Cloud Platform software portfolio.
    """
    upper = sku.upper()

    # Hardware / appliance node licenses — not software products
    if any(upper.startswith(p) for p in (
        "L-PRO-", "L-ULT-", "L-STR-", "L-SW-",
        "DELL-LIC-", "LEN-LIC-", "HPE-LIC-", "CISCO-LIC-",
    )):
        return "_hardware", ""

    for prefix, prod, tier in _LICENSE_SKU_MAP:
        if upper.startswith(prefix):
            lt = (license_type or "").lower()
            if not tier:
                if "ultimate" in lt or "ult" in upper:
                    tier = "Ultimate"
                elif "pro" in lt:
                    tier = "Pro"
                elif "starter" in lt or "str" in upper:
                    tier = "Starter"
            return prod, tier

    return "_other", ""


_SKU_PREFIX_MAP = [
    ("SW-NCP-NCI", "NCI (Cloud Infrastructure)"),
    ("SW-NCP-NCM", "NCM (Cloud Manager)"),
    ("SW-VDI", "VDI"),
    ("SW-FILES", "Files"),
    ("SW-CALM", "Calm"),
    ("SW-NCM", "NCM (Cloud Manager)"),
    ("SW-NCI", "NCI (Cloud Infrastructure)"),
    ("SW-AOS", "AOS"),
    ("SW-ERA", "Era"),
    ("SW-FLOW", "Flow"),
    ("SW-OBJ", "Objects"),
    ("SW-SEC", "Security Central"),
    ("SW-LEAP", "Leap"),
    ("SW-NKE", "NKE (Kubernetes)"),
    ("SW-MINE", "Mine"),
    ("SW-ULT", "AOS (Ultimate)"),
    ("SW-PRO", "AOS (Pro)"),
    ("SWA-NCM", "NCM (Cloud Manager)"),
    ("SWA-NCI", "NCI (Cloud Infrastructure)"),
    ("SWA-AOS", "AOS"),
    ("NX-", "Hardware"),
]

_NAME_KEYWORDS = [
    ("CALM", "Calm"),
    ("ERA", "Era"),
    ("FILES", "Files"),
    ("FLOW", "Flow"),
    ("OBJECTS", "Objects"),
    ("PRISM PRO", "NCM (Cloud Manager)"),
    ("NCM", "NCM (Cloud Manager)"),
    ("MINE", "Mine"),
    ("LEAP", "Leap"),
    ("NKE", "NKE (Kubernetes)"),
    ("KUBERNETES", "NKE (Kubernetes)"),
    ("VDI", "VDI"),
    ("SECURITY CENTRAL", "Security Central"),
]


_RENEWAL_SKU_MAP = [
    ("RSW-NCP-NCI", "NCI"), ("RSW-NCI", "NCI"),
    ("RSW-NCP-NCM", "NCM"), ("RSW-NCM", "NCM"),
    ("RSW-NCP-PRO", "NCI"),  # NCP Pro bundle → NCI
    ("RSW-NCP-ULT", "NCI"),  # NCP Ultimate bundle → NCI
    ("RSW-NKP", "NKP"),
    ("RSW-NUS", "NUS"), ("RSW-FILES", "NUS"), ("RSW-OBJ", "NUS"),
    ("RSW-NDB", "NDB"),
    ("RSW-NAI", "NAI"),
    ("RSW-NC2", "NC2"), ("RSW-NDK", "NDK"), ("RSW-NDL", "NDL"),
    ("R-SW-NCI", "NCI"), ("R-SW-NCP-NCI", "NCI"),
    ("R-SW-NCM", "NCM"), ("R-SW-NCP-NCM", "NCM"),
    ("R-SW-NKP", "NKP"),
    ("R-SW-NUS", "NUS"), ("R-SW-FILES", "NUS"), ("R-SW-OBJ", "NUS"),
    ("R-SW-NDB", "NDB"),
    ("R-SW-NAI", "NAI"),
    ("R-SW-AOS", "NCI"),
    ("R-SW-PRS", "NCM"),  # Prism Pro → NCM
    ("R-SW-CALM", "NCM"),
    ("R-SW-ERA", "NDB"),
    ("R-SW-FLOW", "NCI"),
    ("R-SW-SEC", "NCM"),
    ("R-SW-VDI", "NCI-VDI"),
    ("R-L-CORES", "NCI"),
    ("R-L-FLASH", "NCI"),
    ("R-L-NCI", "NCI"),
    ("R-L-NCM", "NCM"),
    ("R-L-NUS", "NUS"),
    ("R-L-NDB", "NDB"),
]

_RENEWAL_FAMILY_KEYWORDS = {
    "Software -Prism Pro": "NCM",
    "Software License Bundle": None,
    "Internal Accounting": None,
    "System Support": "Support",
    "Hardware Support": "Support",
}


def _classify_renewal_product(product_name: str, product_family: str) -> str:
    """Map a renewal line item product name to an NCP software family."""
    upper = product_name.upper()
    for prefix, fam in _RENEWAL_SKU_MAP:
        if upper.startswith(prefix):
            return fam
    mapped = _RENEWAL_FAMILY_KEYWORDS.get(product_family)
    if mapped is not None:
        return mapped
    if product_family and product_family not in ("Software License Bundle", "Internal Accounting"):
        return product_family
    return "Other"


def _categorize_product(name: str, family: str) -> str:
    upper = name.upper()
    for prefix, cat in _SKU_PREFIX_MAP:
        if upper.startswith(prefix):
            return cat
    for kw, cat in _NAME_KEYWORDS:
        if kw in upper:
            return cat
    if family and family != "Software License Bundle":
        return family
    return "Other"
