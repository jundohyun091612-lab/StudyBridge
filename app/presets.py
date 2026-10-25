"""
presets.py
- Static catalog of COMMON US K-12 SUBJECTS, written from general knowledge.
- NOT a copy of any school's or state's official curriculum.
- ONE entry per subject. The different "versions" of a subject (Standard / Honors /
  AP / a sequence level) are LEVEL OPTIONS inside that subject, not separate cards.
  So "Biology" has levels [Standard, Honors, AP] instead of a separate "AP Biology" card.
- Subjects are grouped by AREA so the picker reads cleanly.
- Chapter lists are generated on demand by ai.generate_syllabus(name, grade, level).
"""

ELEMENTARY = "elementary"
MIDDLE = "middle"
HIGH = "high"

STD = "Standard"
HON = "Honors"
AP = "AP"

AREA_ORDER = ["Core", "Mathematics", "Science", "English / Language Arts",
              "Social Studies", "World Languages", "Computer Science & Arts"]


def school_level_for_grade(grade):
    try:
        g = int(grade)
    except (TypeError, ValueError):
        return HIGH
    if g <= 5:
        return ELEMENTARY
    if g <= 8:
        return MIDDLE
    return HIGH


CATALOG = {
    ELEMENTARY: [
        {"name": "Math",                 "area": "Core", "levels": [STD], "grades": [0,1,2,3,4,5]},
        {"name": "Reading & Language Arts", "area": "Core", "levels": [STD], "grades": [0,1,2,3,4,5]},
        {"name": "Writing",              "area": "Core", "levels": [STD], "grades": [2,3,4,5]},
        {"name": "Science",              "area": "Core", "levels": [STD], "grades": [0,1,2,3,4,5]},
        {"name": "Social Studies",       "area": "Core", "levels": [STD], "grades": [0,1,2,3,4,5]},
        {"name": "Spelling & Vocabulary", "area": "Core", "levels": [STD], "grades": [1,2,3,4,5]},
    ],
    MIDDLE: [
        {"name": "Mathematics",          "area": "Mathematics", "levels": [STD, HON], "grades": [6,7,8]},
        {"name": "Pre-Algebra",          "area": "Mathematics", "levels": [STD, HON], "grades": [7,8]},
        {"name": "Algebra 1",            "area": "Mathematics", "levels": [STD, HON], "grades": [8]},
        {"name": "English / Language Arts", "area": "English / Language Arts", "levels": [STD, HON], "grades": [6,7,8]},
        {"name": "Science",              "area": "Science", "levels": [STD, HON], "grades": [6,7,8]},
        {"name": "World History / Geography", "area": "Social Studies", "levels": [STD, HON], "grades": [6,7]},
        {"name": "US History",           "area": "Social Studies", "levels": [STD, HON], "grades": [8]},
        {"name": "Spanish",              "area": "World Languages", "levels": ["Spanish 1", "Spanish 2"], "grades": [7,8]},
        {"name": "French",               "area": "World Languages", "levels": ["French 1", "French 2"], "grades": [7,8]},
    ],
    HIGH: [
        # Mathematics
        {"name": "Algebra 1",    "area": "Mathematics", "levels": [STD, HON], "grades": [9]},
        {"name": "Geometry",     "area": "Mathematics", "levels": [STD, HON], "grades": [9,10]},
        {"name": "Algebra 2",    "area": "Mathematics", "levels": [STD, HON], "grades": [10,11]},
        {"name": "Pre-Calculus", "area": "Mathematics", "levels": [STD, HON, "AP Precalculus"], "grades": [11,12]},
        {"name": "Calculus",     "area": "Mathematics", "levels": ["AP Calculus AB", "AP Calculus BC"], "grades": [11,12]},
        {"name": "Statistics",   "area": "Mathematics", "levels": [STD, "AP Statistics"], "grades": [11,12]},
        # Science
        {"name": "Biology",      "area": "Science", "levels": [STD, HON, AP], "grades": [9,10]},
        {"name": "Chemistry",    "area": "Science", "levels": [STD, HON, AP], "grades": [10,11]},
        {"name": "Physics",      "area": "Science",
         "levels": [STD, HON, "AP Physics 1", "AP Physics 2", "AP Physics C: Mechanics", "AP Physics C: E&M"], "grades": [11,12]},
        {"name": "Environmental Science", "area": "Science", "levels": [STD, AP], "grades": [10,11,12]},
        # English
        {"name": "English",      "area": "English / Language Arts",
         "levels": [STD, HON, "AP Language", "AP Literature"], "grades": [9,10,11,12]},
        # Social studies
        {"name": "World History", "area": "Social Studies", "levels": [STD, HON, AP], "grades": [9,10]},
        {"name": "US History",   "area": "Social Studies", "levels": [STD, HON, AP], "grades": [11]},
        {"name": "European History", "area": "Social Studies", "levels": [AP], "grades": [10,11,12]},
        {"name": "Government",   "area": "Social Studies",
         "levels": [STD, "AP US Government & Politics", "AP Comparative Government"], "grades": [11,12]},
        {"name": "Economics",    "area": "Social Studies",
         "levels": [STD, "AP Macroeconomics", "AP Microeconomics"], "grades": [11,12]},
        {"name": "Human Geography", "area": "Social Studies", "levels": [STD, AP], "grades": [9,10]},
        {"name": "Psychology",   "area": "Social Studies", "levels": [STD, AP], "grades": [10,11,12]},
        # World languages
        {"name": "Spanish",      "area": "World Languages",
         "levels": ["Spanish 1", "Spanish 2", "Spanish 3", "AP Spanish Language"], "grades": [9,10,11,12]},
        {"name": "French",       "area": "World Languages",
         "levels": ["French 1", "French 2", "French 3", "AP French Language"], "grades": [9,10,11,12]},
        # CS & arts
        {"name": "Computer Science", "area": "Computer Science & Arts",
         "levels": ["AP Computer Science Principles", "AP Computer Science A"], "grades": [9,10,11,12]},
        {"name": "Art History",  "area": "Computer Science & Arts", "levels": [AP], "grades": [10,11,12]},
    ],
}


def courses_for_grade(grade):
    """Return (school_level, groups); groups = ordered [{area, courses:[{name, levels, recommended}]}]."""
    lvl = school_level_for_grade(grade)
    try:
        g = int(grade)
    except (TypeError, ValueError):
        g = None
    courses = CATALOG.get(lvl, [])
    by_area = {}
    for c in courses:
        rec = g is not None and g in c["grades"]
        by_area.setdefault(c["area"], []).append(
            {"name": c["name"], "levels": c["levels"], "recommended": rec})
    groups = []
    for area in AREA_ORDER:
        if area in by_area:
            groups.append({"area": area, "courses": by_area[area]})
    for area, cs in by_area.items():
        if area not in AREA_ORDER:
            groups.append({"area": area, "courses": cs})
    return lvl, groups
