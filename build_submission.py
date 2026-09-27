import json
from pathlib import Path
import sys

# Add root directory to path to allow imports from app/
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot import compose

def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def build_submission():
    dataset_dir = ROOT / "dataset" / "expanded"
    test_pairs_path = dataset_dir / "test_pairs.json"
    
    if not test_pairs_path.exists():
        print(f"Error: {test_pairs_path} not found.")
        sys.exit(1)
        
    with open(test_pairs_path, 'r', encoding='utf-8') as f:
        test_pairs = json.load(f).get("pairs", [])
        
    submission_path = ROOT / "submission.jsonl"
    
    with open(submission_path, 'w', encoding='utf-8') as out_f:
        for pair in test_pairs:
            test_id = pair.get("test_id")
            trigger_id = pair.get("trigger_id")
            merchant_id = pair.get("merchant_id")
            customer_id = pair.get("customer_id")
            
            trigger = load_json(dataset_dir / "triggers" / f"{trigger_id}.json")
            merchant = load_json(dataset_dir / "merchants" / f"{merchant_id}.json")
            
            if not merchant:
                print(f"Warning: Merchant {merchant_id} not found.")
                continue
                
            category_slug = merchant.get("category_slug")
            category = load_json(dataset_dir / "categories" / f"{category_slug}.json")
            
            customer = None
            if customer_id:
                customer = load_json(dataset_dir / "customers" / f"{customer_id}.json")
                
            if not category or not trigger:
                print(f"Warning: Missing data for {test_id} (trigger: {trigger_id}, category: {category_slug})")
                continue
                
            # Call our compose function
            try:
                result = compose(category, merchant, trigger, customer)
                
                # Write to jsonl
                out_line = {
                    "test_id": test_id,
                    "body": result["body"],
                    "cta": result["cta"],
                    "send_as": result["send_as"],
                    "suppression_key": result["suppression_key"],
                    "rationale": result["rationale"]
                }
                out_f.write(json.dumps(out_line, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"Error processing {test_id}: {e}")
                
    print(f"Successfully generated {submission_path} with {len(test_pairs)} entries.")

if __name__ == "__main__":
    build_submission()
