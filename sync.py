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
 
# ╔════════════════════════════════════════════════════════════════════╗
# ║                  EDIT THIS SECTION EACH YEAR                      ║
# ║  Update your classes and teachers below when your schedule changes ║
# ╚════════════════════════════════════════════════════════════════════╝
 
# PROFESSOR OVERRIDES
# If Canvas shows the wrong teacher for a class, add it here.
# The course name must match EXACTLY how it appears on Canvas.
# To find the exact name, check the sync logs or your Canvas dashboard.
#
# Format:  "Course Name On Canvas": "Correct Teacher Name",
#
# Example:
#   "IB Math AI HLY2 -- Smith": "John Smith",
#   "AP English Lit-Jones": "Sarah Jones",
 
PROFESSOR_OVERRIDES = {
    # ── 2025-2026 School Year ──
    "IB Lng&Lit HLY1-Stevenson": "Angela Stevenson",
    "Digital Photo 1-P 1 & 3": "Christina Salinas",
    # Add more overrides below as needed:
    # "Course Name": "Teacher Name",
}
 
# COURSES TO SKIP
# Add any course names you want to completely ignore (no assignments synced).
# Useful for homeroom, advisory, or non-academic courses.
#
# Format: "Course Name On Canvas",
 
COURSES_TO_SKIP = [
    "DMHS Class of 2027 (Juniors)",
    # Add more courses to skip below:
    # "Course Name",
]
 
 
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
        "Assignment Name": {"title": [{"text": {"content": name[:2000]}}]},
        "Class": {"select": {"name": course_name[:100]}},
        "Professor": {"select": {"name": professor_name[:100]}},
        "Notes": {"rich_text": [{"text": {"content": notes}}]},
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
            professor = get_course_teacher(course_id)
        print(f"   Professor: {professor}")
 
        # Get assignments
        assignments = get_assignments(course_id)
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
                    error_count += 1
                    if error_count <= 3:
                        print(f"   ⚠ Failed to create '{name}': {e}")
                    elif error_count == 4:
                        print(f"   ... suppressing further errors (same issue)")
 
        print()
 
    print("─" * 40)
    print(f"✅ Sync complete!")
    print(f"   Created: {created_count}")
    print(f"   Updated: {updated_count}")
    print(f"   Skipped (past due): {skipped_count}")
    print(f"   Total courses: {len(courses)}")
 
 
if __name__ == "__main__":
    sync()
 
