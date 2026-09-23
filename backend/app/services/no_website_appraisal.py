"""Deterministic no-website appraisal and outreach templates."""
from __future__ import annotations

import json
from typing import Any, Dict, List


def appraise_no_website(
    business_name: str,
    niche: str,
    location: str,
    search_evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return a rule-based no-website report using organic search evidence."""
    search_evidence = search_evidence or {}
    business_results = _business_results(search_evidence)
    competitors = [
        {
            "name": result.get("title") or result.get("domain") or "Visible business website",
            "website": result.get("link") or "",
            "strength": "Shows up with an owned website in the checked organic results.",
        }
        for result in business_results[:3]
    ]
    query = search_evidence.get("query") or f"{niche} in {location}".strip()
    has_verified_examples = bool(competitors)
    missed = [
        f"People searching '{query}' can find owned websites from other businesses, while {business_name} has no owned website to send them to.",
        "Without a website, searchers are left with third-party profiles instead of a page the business controls.",
        "A simple service page could explain the offer, build trust, and give prospects a clear way to request a quote.",
    ]
    if not has_verified_examples:
        missed[0] = (
            f"{business_name} has no owned website for people searching around "
            f"'{query}', which limits what the business can control in organic search."
        )

    recommendations = [
        "Launch a focused website with services, trust proof, contact details, and a quote path.",
        "Use the page to answer the questions prospects usually need before contacting a provider.",
        "Keep the first page built around one action: request a quote or make contact.",
    ]
    findings = [
        {
            "category": "owned visibility",
            "observation": "the business has no owned website connected to the lead record",
            "business_impact": "searchers may only see third-party listings that the business cannot fully control.",
            "recommendation": recommendations[0],
        },
        {
            "category": "conversion path",
            "observation": "there is no owned page dedicated to turning search interest into an enquiry",
            "business_impact": "ready buyers have fewer reasons and fewer routes to contact the business.",
            "recommendation": recommendations[2],
        },
    ]
    if has_verified_examples:
        domains = ", ".join(
            str(result.get("domain") or result.get("root_domain") or result.get("link"))
            for result in business_results[:3]
            if result.get("domain") or result.get("root_domain") or result.get("link")
        )
        findings.insert(1, {
            "category": "search evidence",
            "observation": f"business-owned websites are visible in organic results for this search: {domains}",
            "business_impact": "the search page already shows that owned websites can appear for this kind of query.",
            "recommendation": "Give the business a focused owned page that can compete for the same kind of search intent.",
        })

    return {
        "competitors": competitors,
        "missed_opportunities": missed,
        "potential_revenue_impact": (
            "The practical risk is missed calls and quote requests from prospects who want to check a business before contacting it."
        ),
        "recommendations": recommendations,
        "findings": findings,
        "search_evidence": search_evidence,
        "appraisal_method": "deterministic",
        "raw_analysis": json.dumps(
            {
                "method": "deterministic_no_website_appraisal_v1",
                "business_name": business_name,
                "query": query,
                "verified_business_result_count": len(business_results),
                "findings": findings,
            },
            ensure_ascii=True,
            default=str,
        ),
    }


def generate_no_website_email_template(
    business_name: str,
    location: str,
    niche: str,
    no_website_data: Dict[str, Any],
    generated_website_url: str | None = None,
) -> Dict[str, str]:
    """Build a no-website cold email from deterministic findings."""
    subject = "website idea"
    search_evidence = no_website_data.get("search_evidence") or {}
    query = search_evidence.get("query") or f"{niche} in {location}".strip()
    examples = _business_names(no_website_data.get("competitors") or [])
    if examples:
        examples_text = _human_list(examples[:3])
        search_context = (
            f"When I checked Google for the same search, I noticed businesses like "
            f"{examples_text} showing websites where customers can compare services and contact details."
        )
    else:
        search_context = (
            "When I checked Google for the same search, I saw how much the results lean on websites, "
            "directories, and listings that help customers compare options."
        )
    preview_line = (
        f"So I put together a simple example of what that could look like:\n\n{generated_website_url}"
        if generated_website_url
        else "A useful next step would be a simple page built around services, trust signals, and quote requests."
    )
    body = "\n\n".join([
        f"Hi {business_name} team,",
        f"I was looking at how customers search for {query} and came across {business_name} on Google Maps.",
        search_context,
        "I couldn't find a website for your business in those results.",
        preview_line,
        "It's only a prototype, but I can customize it further around your services, photos, and preferred style.",
        "Is getting customers through Google search something you're interested in, or has Maps been handling that well enough so far?",
    ])
    return {"subject": subject, "body": body}


def _primary_finding(no_website_data: Dict[str, Any]) -> Dict[str, str]:
    findings = no_website_data.get("findings") or []
    if findings:
        first = findings[0]
        return {
            "observation": str(first.get("observation") or "the business does not have an owned website").strip(),
            "business_impact": str(first.get("business_impact") or "That makes it harder to turn search interest into enquiries.").strip(),
        }
    return {
        "observation": "the business does not have an owned website",
        "business_impact": "That makes it harder to turn search interest into enquiries.",
    }


def _business_results(search_evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = search_evidence.get("business_results")
    if isinstance(results, list):
        return [result for result in results if isinstance(result, dict)]
    results = search_evidence.get("organic_results")
    if isinstance(results, list):
        return [result for result in results if isinstance(result, dict)][:3]
    return []


def _business_names(competitors: List[Dict[str, Any]]) -> List[str]:
    names = []
    for competitor in competitors:
        name = str(competitor.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _human_list(values: List[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"
