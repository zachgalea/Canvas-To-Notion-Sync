"""
Canvas LMS → Notion Database Sync Script
Automatically fetches assignments from TWO Canvas schools/accounts and
syncs each one into its own Notion database.
Designed to run on GitHub Actions on a schedule (cron).
"""

import os
import requests
from datetime import datetime, timezone

# ─── Configuration (pulled from environment variables) ───

NOTION_SECRET = os.environ["NOTION_SECRET"]
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_SECRET}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Each school is its own Canvas account (different base URL + token)
# syncing into its own Notion database.
SCHOOLS = [
    {
        "label": "School 1",
        "canvas_base_url": os.environ["CANVAS_BASE_URL"].rstrip("/"),
        "canvas_token": os.environ["CANVAS_TOKEN"],
        "notion_database_id": os.environ["NOTION_DATABASE_ID"],
        "title_property": "Title",       # name of the title column in this database
        "include_professor": False,       # this database has no Professor column
    },
    {
        "label": "School 2",
        "canvas_base_url": os.environ["CANVAS_BASE_URL_2"].rstrip("/"),
        "canvas_token": os.environ["CANVAS_TOKEN_2"],
        "notion_database_id": os.environ["NOTION_DATABASE_ID_2"],
        "title_property": "Assignment Name",  # name of the title column in this database
        "include_professor": True,             # this database has a Professor column
    },
]

# ╔════════════════════════════════════════════════════════════════════╗
# ║                  EDIT THIS SECTION EACH YEAR                      ║
# ║  Update your classes and teachers below when your schedule changes ║
# ╚════════════════════════════════════════════════════════════════════╝

# PROFESSOR OVERRIDES
# If Canvas shows the wrong teacher for a class, add it here.
# The course name must match EXACTLY how it appears on Canvas.
# To find the exact name, check the sync logs or your Canvas dashboard.
# Works for courses from EITHER school — just add the exact name.
#
# Format:  "Course Name On Canvas": "Correct Teacher Name",
#
# Example:
#   "IB Math AI HLY2 -- Smith": "John Smith",
#   "AP English Lit-Jones": "Sarah Jones",

PROFESSOR_OVERRIDES = {
    # ── 2026-2027 School Year ──
    "IB Bio HL (Y2)-Iyengar": "Iyengar",
    "Military Sci 4-Levesque": "Levesque",
    "IB Lng&Lit HLY2-Chatfield": "Chatfield",
    "IB HOTA HL (Y2)-Schwartz": "Schwartz",
    "IntroWeightTrng-Ghazanfari": "Ghazanfari",

    # Add more overrides below as needed (from either school):
    # "Course Name": "Teacher Name",
}

# COURSES TO SKIP
# Add any course names you want to completely ignore (no assignments synced).
# Useful for homeroom, advisory, or non-academic courses.
# Works for courses from EITHER school — just add the exact name.
#
# Format: "Course Name On Canvas",

COURSES_TO_SKIP = [
    "DMHS Class of 2027 (Seniors)",
    "Military Sci 4-Levesque",
    # Add more courses to skip below:
    # "Course Name",
]


# ─── Canvas API Helpers ───

def canvas_get(base_url, headers, endpoint):
    """Fetch all pages from a Canvas API endpoint."""
    url = f"{base_url}/api/v1/{endpoint}"
    results = []
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        results.extend(resp.json())
        # Handle pagination
        url = resp.links.get("next", {}).get("url")
    return results


def get_active_courses(base_url, headers):
    """Get all active courses for the current user on this Canvas account."""
    courses = canvas_get(base_url, headers, "courses?enrollment_state=active&per_page=100")
    return [c for c in courses if isinstance(c, dict) and c.get("name")]


def get_course_teacher(base_url, headers, course_id):
    """Get the primary teacher/professor name for a course."""
    try:
        enrollments = canvas_get(
            base_url, headers,
            f"courses/{course_id}/enrollments?type[]=TeacherEnrollment&per_page=5"
        )
        for e in enrollments:
            name = e.get("user", {}).get("name")
            if name:
                return name
    except Exception:
        pass
    return "Unknown"


def get_assignments(base_url, headers, course_id):
    """Get all assignments for a course."""
    try:
        return canvas_get(base_url, headers, f"courses/{course_id}/assignments?per_page=100")
    except Exception as e:
        print(f"  ⚠ Could not fetch assignments for course {course_id}: {e}")
        return []


# ─── Notion API Helpers ───

def get_existing_assignments(database_id, title_property):
    """Fetch all existing assignment entries from a Notion database.
    Returns a dict mapping (class_name, assignment_name) → page_id.
    """
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
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
            title_prop = props.get(title_property, {})
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
    """Create a new page in a Notion database."""
    url = "https://api.notion.com/v1/pages"
    resp = requests.post(url, headers=NOTION_HEADERS, json=assignment_data, timeout=30)
    if not resp.ok:
        print(f"      Notion error: {resp.text}")
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

def build_properties(assignment, course_name, professor_name, title_property, include_professor):
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

    # Notes — just the assignment description, cleaned up
    notes = ""
    description = assignment.get("description") or ""
    if description:
        import re
        # Strip HTML tags for a clean text snippet
        clean = re.sub(r"<[^>]+>", "", description).strip()
        # Collapse multiple whitespace/newlines into single spaces
        clean = re.sub(r"\s+", " ", clean)
        if clean:
            notes = clean[:2000]

    # Build Notion properties
    properties = {
        title_property: {"title": [{"text": {"content": name[:2000]}}]},
        "Class": {"select": {"name": course_name[:100]}},
        "Notes": {"rich_text": [{"text": {"content": notes}}]},
    }

    if include_professor:
        properties["Professor"] = {"select": {"name": professor_name[:100]}}

    if due_date:
        properties["Due Date"] = {"date": {"start": due_date}}

    if date_assigned:
        properties["Date Assigned"] = {"date": {"start": date_assigned}}

    return properties


# ─── Startup Validation ───

def validate_school(school):
    """Run quick checks before syncing so setup mistakes fail with a clear
    message instead of a wall of raw API errors partway through."""
    label = school["label"]
    base_url = school["canvas_base_url"]
    headers = {"Authorization": f"Bearer {school['canvas_token']}"}
    database_id = school["notion_database_id"]
    title_property = school["title_property"]
    include_professor = school["include_professor"]

    # Check Canvas connectivity/token
    try:
        resp = requests.get(
            f"{base_url}/api/v1/courses?per_page=1", headers=headers, timeout=15
        )
        if resp.status_code == 401:
            raise RuntimeError(
                f"[{label}] Canvas token was rejected (401). It's likely expired "
                f"or revoked — generate a new one in Canvas → Account → Settings "
                f"→ Approved Integrations, and update the CANVAS_TOKEN secret."
            )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"[{label}] Could not reach Canvas at {base_url}. "
            f"Double check the CANVAS_BASE_URL secret is correct."
        )

    # Check Notion database is reachable (integration connected + valid ID)
    resp = requests.get(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers=NOTION_HEADERS,
        timeout=15,
    )
    if resp.status_code == 404:
        raise RuntimeError(
            f"[{label}] Notion database {database_id} not found (404). "
            f"Either the NOTION_DATABASE_ID secret is wrong (make sure you copied "
            f"the database's own ID, not a parent page's), or your Notion "
            f"integration isn't connected to this database — check '...' → "
            f"Connections on the database page."
        )
    resp.raise_for_status()
    schema = resp.json().get("properties", {})

    # Check every property the script needs actually exists in this database
    required = [title_property, "Class", "Notes", "Due Date", "Date Assigned"]
    if include_professor:
        required.append("Professor")

    missing = [p for p in required if p not in schema]
    if missing:
        raise RuntimeError(
            f"[{label}] Notion database is missing expected propert"
            f"{'y' if len(missing) == 1 else 'ies'}: {', '.join(missing)}. "
            f"Either add {'it' if len(missing) == 1 else 'them'} to the database, "
            f"or update the school's config in sync.py to match your actual column names."
        )


# ─── Per-School Sync Logic ───

def sync_school(school):
    """Sync one Canvas account's assignments into its own Notion database."""
    validate_school(school)

    label = school["label"]
    base_url = school["canvas_base_url"]
    headers = {"Authorization": f"Bearer {school['canvas_token']}"}
    database_id = school["notion_database_id"]
    title_property = school["title_property"]
    include_professor = school["include_professor"]

    print(f"═══ {label} ═══")
    print(f"   Canvas: {base_url}")
    print(f"   Database: {database_id}")
    print()

    # 1. Get existing Notion entries to avoid duplicates
    print("📋 Fetching existing Notion entries...")
    existing = get_existing_assignments(database_id, title_property)
    print(f"   Found {len(existing)} existing entries")
    print()

    # 2. Fetch active courses
    print("📚 Fetching active courses...")
    courses = get_active_courses(base_url, headers)
    print(f"   Found {len(courses)} active courses")
    print()

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    for course in courses:
        course_id = course["id"]
        course_name = course["name"]
        print(f"── {course_name} ──")

        # Skip courses in the skip list
        if course_name in COURSES_TO_SKIP:
            print(f"   ⏭ Skipped (in COURSES_TO_SKIP)")
            print()
            continue

        # Get professor (check overrides first)
        if course_name in PROFESSOR_OVERRIDES:
            professor = PROFESSOR_OVERRIDES[course_name]
        else:
            professor = get_course_teacher(base_url, headers, course_id)
        print(f"   Professor: {professor}")

        # Get assignments
        assignments = get_assignments(base_url, headers, course_id)
        print(f"   Assignments: {len(assignments)}")

        for assignment in assignments:
            name = assignment.get("name", "Untitled")
            key = (course_name, name)

            # Skip past-due assignments that aren't already in the database
            due_at = assignment.get("due_at")
            if due_at and key not in existing:
                try:
                    due_date = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
                    if due_date < datetime.now(timezone.utc):
                        skipped_count += 1
                        continue
                except (ValueError, TypeError):
                    pass

            properties = build_properties(
                assignment, course_name, professor, title_property, include_professor
            )

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
                    "parent": {"database_id": database_id},
                    "properties": properties,
                }
                try:
                    create_notion_page(page_data)
                    created_count += 1
                    print(f"   ✅ Created: {name}")
                except Exception as e:
                    error_count += 1
                    if error_count <= 3:
                        print(f"   ⚠ Failed to create '{name}': {e}")
                    elif error_count == 4:
                        print(f"   ... suppressing further errors (same issue)")

        print()

    print("─" * 40)
    print(f"✅ {label} sync complete!")
    print(f"   Created: {created_count}")
    print(f"   Updated: {updated_count}")
    print(f"   Skipped (past due): {skipped_count}")
    print(f"   Total courses: {len(courses)}")
    print()


# ─── Main ───

def sync():
    print("🔄 Starting Canvas → Notion sync...")
    print()
    for school in SCHOOLS:
        try:
            sync_school(school)
        except Exception as e:
            print(f"❌ {school['label']} sync failed: {e}")
            print()


if __name__ == "__main__":
    sync()
