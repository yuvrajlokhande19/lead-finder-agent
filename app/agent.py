# ruff: noqa

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models.lite_llm import LiteLlm

from app.tools import (
    analyze_lead_pipeline,
    enrich_contact,
    get_all_leads,
    get_lead,
    search_businesses,
)

MODEL = "ollama_chat/gemma4"


def tool_search_businesses(business_type: str, location: str, max_results: int = 20) -> dict:
    """Search Google Places for medium-scale businesses of a given type in a
    location and save them as leads.

    Args:
        business_type: e.g. "dental clinic", "gym", "accounting firm".
        location: city, area or landmark, e.g. "Koramangala, Bangalore".
        max_results: how many results to fetch (max 20).

    Returns:
        Dict with status and count of new leads saved.
    """
    return search_businesses(business_type, location, max_results)


def tool_analyze_lead(lead_id: int) -> dict:
    """Run full analysis on a saved lead: web enrichment for the contact person,
    a UI/UX website-design prompt, and an outreach email draft.

    Args:
        lead_id: numeric id of the lead in the database.

    Returns:
        Dict with status, enriched lead data, likely_contact_person.
    """
    return analyze_lead_pipeline(int(lead_id))


def tool_list_leads() -> dict:
    """List all saved leads with basic info."""
    return {"status": "ok", "leads": get_all_leads()}


def tool_get_lead(lead_id: int) -> dict:
    """Get full details of one lead by id, including any stored analysis."""
    lead = get_lead(int(lead_id))
    if lead is None:
        return {"status": "error", "message": f"Lead {lead_id} not found"}
    return {"status": "ok", "lead": lead}


root_agent = Agent(
    name="website_lead_agent",
    model=LiteLlm(model=MODEL),
    instruction="""You are a Lead Generation Assistant for a web design agency.

Your job is to help find medium-scale local businesses (clinics, gyms, firms,
shops, schools etc.) that could benefit from a professional website, and to
prepare everything needed to approach them.

Workflow you follow:
1. When the user gives a business type and a location, call tool_search_businesses.
2. To prepare outreach material for a specific lead, call tool_analyze_lead —
   this finds likely contact people online, writes a UI/UX design prompt for
   their future website, and drafts a personalized outreach email.
3. Use tool_list_leads / tool_get_lead to show stored leads at any time.

Guidelines:
- Focus on small-to-medium businesses, not large chains or franchises.
- Always report how many new leads were found vs already known.
- When showing a lead summary, include name, type, address, phone and whether
  they have a website.
- Be concise and businesslike.""",
    tools=[
        tool_search_businesses,
        tool_analyze_lead,
        tool_list_leads,
        tool_get_lead,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
