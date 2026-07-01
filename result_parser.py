import re
import hashlib
import json
import os
from config import DATA_DIR

def parse_announcement(text: str):
    """
    Parses the ET announcement format and extracts metadata and candidate list.
    """
    result = {
        "position": "",
        "location": "",
        "announcement": "",
        "description": "",
        "date_time": "",
        "candidates": []
    }

    # Extract Metadata using regex
    pos_match = re.search(r"Postion\s*:\s*(.*)", text, re.IGNORECASE)
    if pos_match:
        result["position"] = pos_match.group(1).strip()

    loc_match = re.search(r"Location\s*:\s*(.*)", text, re.IGNORECASE)
    if loc_match:
        result["location"] = loc_match.group(1).strip()

    ann_match = re.search(r"Announcement\s*:\s*(.*)", text, re.IGNORECASE)
    if ann_match:
        result["announcement"] = ann_match.group(1).strip()

    # Special check for multiple Location/Position labels in the provided text
    # The user's text has labels twice. We take the first one or clean it up.
    
    dt_match = re.search(r"DATE & TIME:\s*(.*)", text, re.IGNORECASE)
    if dt_match:
        result["date_time"] = dt_match.group(1).strip()

    # Extract Description
    # Usually between "Description :" and "Candidate_List :"
    desc_match = re.search(r"Description\s*:(.*?)(?=Candidate_List\s*:)", text, re.DOTALL | re.IGNORECASE)
    if desc_match:
        result["description"] = desc_match.group(1).strip()

    # Extract Candidates
    # Look for lines starting with numbers after "Candidate_List :"
    cand_section = re.search(r"Candidate_List\s*:(.*)", text, re.DOTALL | re.IGNORECASE)
    if cand_section:
        cand_text = cand_section.group(1).strip()
        # Regex to match: Number Name (ignoring the NO NAME header)
        lines = cand_text.splitlines()
        for line in lines:
            line = line.strip()
            # Match "1 NAME" or "1. NAME" or "1\tNAME"
            match = re.match(r"^(\d+)\s+(.+)", line)
            if match:
                result["candidates"].append({
                    "no": match.group(1),
                    "name": match.group(2).strip()
                })

    # Generate Unique ID
    id_base = f"{result['position']}-{result['announcement']}-{result['date_time']}"
    result["id"] = hashlib.md5(id_base.encode()).hexdigest()[:12]
    
    return result

def save_result(result: dict, data_dir: str = None):
    if data_dir is None:
        data_dir = os.path.join(DATA_DIR, "announcements")
    os.makedirs(data_dir, exist_ok=True)
    filename = os.path.join(data_dir, f"{result['id']}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return filename

if __name__ == "__main__":
    # Test with provided sample
    sample_text = """
Postion :   LABORATORY TECHNICIAN
Location :    ETHIOPIAN AVIATION UNIVERSITY, STUDENT CAFETERIA
Announcement :    WRITTEN EXAM
Description :
          CALL FOR WRITTEN EXAM    

                           (LABORATORY TECHNICIAN)
...
Candidate_List :
NO	NAME
1	ABEBA EPHREM WONDIFRAW
2	ABSERASH AYELE TSEGAYE
"""
    parsed = parse_announcement(sample_text)
    print(json.dumps(parsed, indent=2))
