"""Product lexicon: which product family a fact row (or a question) is about.

Facts are tagged from their table caption + row label + page; questions from their wording.
Families are deliberately coarse; finer distinctions (jumbo/special, purchase/refinance, Roth/Traditional,
term, balance, model year, loan amount) are qualifiers handled in `rates.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    key: str
    name: str
    q: str          # regex over the (lower-cased, digit-normalised) question
    f: str          # regex over "caption | label | page title | url path" of a fact
    kind: str       # "deposit" | "loan" | "card"


# Order matters for facts: first family whose `f` matches wins. Questions may match several.
FAMILIES: list[Family] = [
    Family("launchpad", "Launchpad Savings", r"launch ?pad", r"launchpad", "deposit"),
    Family("bright", "Bright Savings", r"bright savings|online(?:-only)? savings|online savings|bright account",
           r"bright savings|saving account rates.*online|online-savings", "deposit"),
    Family("you_name_it", "You-Name-It Savings", r"you[- ]?name[- ]?it", r"you name it", "deposit"),
    Family("holiday", "Holiday Club Savings", r"holiday|christmas club", r"holiday club", "deposit"),
    Family("hsa", "Health Savings Account", r"\bhsa\b|health savings", r"health savings", "deposit"),
    Family("high_yield", "High-Yield Savings", r"high[- ]?yield savings|high[- ]?yield money|high[- ]?yield account",
           r"high-yield savings|high yield savings", "deposit"),
    Family("achieve", "Achieve Money Market", r"achieve", r"\bachieve\b", "deposit"),
    Family("investors_choice", "Investor's Choice Money Market", r"investor'?s'? choice|money market",
           r"investor'?s choice", "deposit"),
    Family("ira_variable", "Variable-rate IRA", r"(variable|savings) ira|ira savings|variable[- ]rate ira",
           r"variable iras|ira.*\$[\d,]+", "deposit"),
    Family("ira_cert", "IRA Certificate", r"\bira\b|\broth\b|traditional ira|retirement (account|certificate)",
           r"\biras?\b|roth:|traditional:", "deposit"),
    Family("certificate", "Term Share Certificate", r"certificate|\bcd\b|share cert|term share|time deposit",
           r"certificate", "deposit"),
    Family("first_rate_checking", "1st Rate Checking",
           r"(1st|first)[- ]rate|interest checking|high[- ]yield checking|checking.*(rate|apy|interest|dividend|earn)|"
           r"(rate|apy|interest|dividend).*checking",
           r"1st rate checking", "deposit"),
    Family("free_checking", "Free Checking", r"free checking|checking.*(rate|apy|interest|dividend)|"
           r"(rate|apy|interest|dividend).*checking", r"free checking", "deposit"),
    Family("share_savings", "Share Savings",
           r"share savings|regular savings|primary savings|basic savings|savings account|savings tier|"
           r"\bsavings\b(?!.*(bright|online|high|holiday|health|launch|name|money market|bond))",
           r"share savings", "deposit"),
    Family("auto", "Auto & Motorcycle Loan", r"\bauto\b|\bcar\b|vehicle|truck|motorcycle|\bsuv\b",
           r"auto|vehicle|motorcycle", "loan"),
    Family("boat_rv", "Boat & RV Loan", r"\bboat|\brv\b|camper|motor ?home|travel trailer", r"boat", "loan"),
    Family("atv", "ATV, Snowmobile & Jet Ski Loan", r"\batv|snowmobile|jet ?ski|power ?sport|side[- ]by[- ]side|utv",
           r"atv|snowmobile", "loan"),
    Family("home_improvement", "Home Improvement Loan", r"home improvement|renovat|remodel", r"home improvement",
           "loan"),
    Family("heloc", "Home Equity Line of Credit (HELOC)", r"heloc|home equity line|equity line|line of credit.*home",
           r"heloc", "loan"),
    Family("home_equity_loan", "Home Equity Loan (Fixed)", r"home equity loan|equity loan|second mortgage",
           r"home equity loan|loan – standard|loan - standard", "loan"),
    Family("mortgage", "Fixed-rate Mortgage",
           r"mortgage|home loan|house loan|\b\d+[- ]year fixed|\bfixed[- ]rate\b.*(home|house|loan)|refinanc|"
           r"\b(15|20|30)[- ]?(year|yr)\b",
           r"conventional|mortgage rate relief", "loan"),
    Family("personal_loan", "Personal Loan", r"personal loan|signature loan|unsecured loan|debt consolidation loan",
           r"personal loan|compare rates > personal", "loan"),
    Family("business_platinum", "Business Platinum Visa", r"business platinum", r"business platinum", "card"),
    Family("business_rewards", "Business Rewards Visa", r"business rewards", r"business rewards", "card"),
    Family("platinum_visa", "Platinum Visa", r"platinum", r"platinum visa|introductory balance transfer", "card"),
    Family("rewards_visa", "Rewards Visa", r"rewards? (visa|card|credit)|rewards\b", r"rewards visa", "card"),
]

FAMILY_BY_KEY = {f.key: f for f in FAMILIES}

CARD_FAMILIES = ["platinum_visa", "rewards_visa", "business_platinum", "business_rewards"]
CHECKING_FAMILIES = ["free_checking", "first_rate_checking"]

# Products the site describes but for which it publishes no rate. Asking for their rate must be a refusal,
# never a number borrowed from a neighbouring product.
UNPUBLISHED_RATE_PRODUCTS = [
    (r"\b(arm|adjustable)\b|\d+/\d+ (arm|adjustable)|\b\d/1\b", "adjustable-rate mortgage (ARM)"),
    (r"construction", "construction loan"),
    (r"\bland\b|\blot loan", "land/lot loan"),
    (r"\bfha\b|\bva loan|\bva mortgage|\busda\b|government[- ]backed", "government-backed mortgage"),
    (r"jumbo (mortgage|home loan|loan)", "jumbo mortgage"),
    (r"bridge loan", "bridge loan"),
    (r"student loan|private student|college loan", "student loan"),
    (r"business loan|commercial loan|commercial real estate|equipment loan|sba", "business loan"),
    (r"business line of credit|business credit line", "business line of credit"),
    (r"physician|doctor loan|medical professional", "medical professional mortgage"),
]


def family_of_fact(caption: str, label: str, title: str, path: str, context=()) -> str | None:
    hay = f"{caption} | {label} | {title} | {path}".lower().replace("’", "'")
    lab = f"{caption} | {label}".lower().replace("’", "'")
    # The row label/caption is the most specific signal; then the nearest section labels above the
    # table (tab names like "Term-Share Certificate Rates"); page title / url only as a last resort.
    for target in [lab] + [c.lower().replace("’", "'") for c in context] + [hay]:
        for fam in FAMILIES:
            if re.search(fam.f, target):
                if fam.key == "certificate" and re.search(r"\biras?\b|roth|traditional", target):
                    continue
                return fam.key
    return None


def families_in_question(q: str) -> list[str]:
    ql = q.lower()
    out = []
    for fam in FAMILIES:
        if re.search(fam.q, ql):
            out.append(fam.key)
    # disambiguation between overlapping patterns
    s = set(out)
    if "business_platinum" in s:
        s.discard("platinum_visa")
    if "business_rewards" in s:
        s.discard("rewards_visa")
    if "ira_variable" in s or "ira_cert" in s:
        s.discard("certificate")
        s.discard("share_savings")
    if s & {"launchpad", "bright", "you_name_it", "holiday", "hsa", "high_yield", "achieve", "investors_choice"}:
        s.discard("share_savings")
    if "achieve" in s or "high_yield" in s:
        if not re.search(r"investor", ql):
            s.discard("investors_choice")
    if "certificate" in s:
        s.discard("share_savings")
    if s & {"home_improvement", "heloc", "home_equity_loan"}:
        s.discard("mortgage")
    if "heloc" in s and "home_equity_loan" in s and "line" in ql and "equity loan" not in ql:
        s.discard("home_equity_loan")
    if "first_rate_checking" in s and "free_checking" in s:
        if re.search(r"(1st|first)[- ]rate|interest checking|high[- ]yield checking", ql) and "free" not in ql:
            s.discard("free_checking")
        elif "free checking" in ql and not re.search(r"(1st|first)[- ]rate", ql):
            s.discard("first_rate_checking")
    if "boat_rv" in s or "atv" in s:
        s.discard("auto")
    if s & {"auto", "boat_rv", "atv", "personal_loan"} and "mortgage" in s and not re.search(r"mortgage|home", ql):
        s.discard("mortgage")
    if "personal_loan" in s and "auto" in s and "auto" not in ql and "car" not in ql:
        s.discard("auto")
    return [k for k in out if k in s]


def wants_card_list(q: str) -> bool:
    ql = q.lower()
    return bool(re.search(r"credit cards?|visa", ql)) and not re.search(
        r"platinum|rewards|business (platinum|rewards)", ql)


def unpublished_product(q: str) -> str | None:
    ql = q.lower()
    for pat, name in UNPUBLISHED_RATE_PRODUCTS:
        if re.search(pat, ql):
            return name
    return None
