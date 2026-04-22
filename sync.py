"""
Canvas LMS → Notion Database Sync Script
Automatically fetches assignments from Canvas and syncs them to a Notion database.
Designed to run on GitHub Actions on a schedule (cron).
"""

import os
import requests
from datetime import datetime, timezone

# ─── Configuration (pulled from environment variables) ───
CANVAS_BASE_URL = os.environ["CANVAS_BASE_URL"].rstrip("/")
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_SECRET = os.environ["NOTION_SECRET"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

CANVAS_HEADERS = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_SECRET}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


# ─── Canvas API Helpers ───

def canvas_get(endpoint):
    """Fetch all pages from a Canvas API endpoint."""
    url = f"{CANVAS_BASE_URL}/api/v1/{endpoint}"
    results = []
    while url:
        resp = requests.get(url, headers=CANVAS_HEADERS, timeout=30)
        resp.raise_for_status()
        results.extend(resp.json())
        # Handle pagination
        url = resp.links.get("next", {}).get("url")
    return results


def get_active_courses():
    """Get all active courses for the current user."""
    courses = canvas_get("courses?enrollment_state=active&per_page=100")
    return [c for c in courses if isinstance(c, dict) and c.get("name")]


def get_course_teacher(course_id):
    """Get the primary teacher/professor name for a course."""
    try:
        enrollments = canvas_get(
            f"courses/{course_id}/enrollments?type[]=TeacherEnrollment&per_page=5"
        )
        for e in enrollments:
            name = e.get("user", {}).get("name")
            if name:
                return name
    except Exception:
        pass
    return "Unknown"


def get_assignments(course_id):
    """Get all assignments for a course."""
    try:
        return canvas_get(f"courses/{course_id}/assignments?per_page=100")
    except Exception as e:
        print(f"  ⚠ Could not fetch assignments for course {course_id}: {e}")
        return []


# ─── Notion API Helpers ───

def get_existing_assignments():
    """Fetch all existing assignment entries from the Notion database.
    Returns a dict mapping (class_name, assignment_name) → page_id.
    """
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    existing = {}
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = requests.post(url, headers=NOTION_HEADERS, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            props = page.get("properties", {})
            # Extract title
            title_prop = props.get("Assignment Name", {})
            title_parts = title_prop.get("title", [])
            name = title_parts[0]["plain_text"] if title_parts else ""
            # Extract class
            class_prop = props.get("Class", {})
            class_select = class_prop.get("select")
            class_name = class_select["name"] if class_select else ""

            if name:
                existing[(class_name, name)] = page["id"]

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return existing


def create_notion_page(assignment_data):
    """Create a new page in the Notion database."""
    url = "https://api.notion.com/v1/pages"
    resp = requests.post(url, headers=NOTION_HEADERS, json=assignment_data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def update_notion_page(page_id, properties):
    """Update an existing Notion page."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    resp = requests.patch(
        url, headers=NOTION_HEADERS, json={"properties": properties}, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


# ─── Build Notion Properties ───

def build_properties(assignment, course_name, professor_name):
    """Convert a Canvas assignment into Notion database properties."""
    name = assignment.get("name", "Untitled Assignment")

    # Due date
    due_at = assignment.get("due_at")
    due_date = None
    if due_at:
        try:
            due_date = datetime.fromisoformat(due_at.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d"
            )
        except (ValueError, TypeError):
            due_date = None

    # Date assigned (use created_at from Canvas)
    created_at = assignment.get("created_at")
    date_assigned = None
    if created_at:
        try:
            date_assigned = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            date_assigned = None

    # Notes — combine description snippet + submission types
    notes_parts = []
    description = assignment.get("description") or ""
    if description:
        # Strip HTML tags for a clean text snippet
        import re
        clean = re.sub(r"<[^>]+>", "", description).strip()
        if clean:
            notes_parts.append(clean[:500])  # Limit to 500 chars

    sub_types = assignment.get("submission_types", [])
    if sub_types:
        readable = [s.replace("_", " ").title() for s in sub_types]
        notes_parts.append(f"Submission: {', '.join(readable)}")

    points = assignment.get("points_possible")
    if points is not None:
        notes_parts.append(f"Points: {points}")

    notes = " | ".join(notes_parts) if notes_parts else ""

    # Build Notion properties
    properties = {
        "Assignment Name": {"title": [{"text": {"content": name[:2000]}}]},
        "Class": {"select": {"name": course_name[:100]}},
        "Professor": {"select": {"name": professor_name[:100]}},
        "Notes": {"rich_text": [{"text": {"content": notes[:2000]}}]},
    }

    if due_date:
        properties["Due Date"] = {"date": {"start": due_date}}

    if date_assigned:
        properties["Date Assigned"] = {"date": {"start": date_assigned}}

    return properties


# ─── Main Sync Logic ───

def sync():
    print("🔄 Starting Canvas → Notion sync...")
    print(f"   Canvas: {CANVAS_BASE_URL}")
    print(f"   Database: {NOTION_DATABASE_ID}")
    print()

    # 1. Get existing Notion entries to avoid duplicates
    print("📋 Fetching existing Notion entries...")
    existing = get_existing_assignments()
    print(f"   Found {len(existing)} existing entries")
    print()

    # 2. Fetch active courses
    print("📚 Fetching active courses...")
    courses = get_active_courses()
    print(f"   Found {len(courses)} active courses")
    print()

    created_count = 0
    updated_count = 0
    skipped_count = 0

    for course in courses:
        course_id = course["id"]
        course_name = course["name"]
        print(f"── {course_name} ──")

        # Get professor
        professor = get_course_teacher(course_id)
        print(f"   Professor: {professor}")

        # Get assignments
        assignments = get_assignments(course_id)
        print(f"   Assignments: {len(assignments)}")

        for assignment in assignments:
            name = assignment.get("name", "Untitled")
            key = (course_name, name)
            properties = build_properties(assignment, course_name, professor)

            if key in existing:
                # Update existing entry
                try:
                    update_notion_page(existing[key], properties)
                    updated_count += 1
                    print(f"   ✏️  Updated: {name}")
                except Exception as e:
                    print(f"   ⚠ Failed to update '{name}': {e}")
            else:
                # Create new entry
                page_data = {
                    "parent": {"database_id": NOTION_DATABASE_ID},
                    "properties": properties,
                }
                try:
                    create_notion_page(page_data)
                    created_count += 1
                    print(f"   ✅ Created: {name}")
                except Exception as e:
                    print(f"   ⚠ Failed to create '{name}': {e}")

        print()

    print("─" * 40)
    print(f"✅ Sync complete!")
    print(f"   Created: {created_count}")
    print(f"   Updated: {updated_count}")
    print(f"   Total courses: {len(courses)}")


if __name__ == "__main__":
    sync()
