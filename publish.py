import sys
import os
from result_parser import parse_announcement, save_result
from config import BASE_URL

def main():
    print("="*60)
    print("ET Results Publisher Tool")
    print("="*60)
    print("\nPlease paste the announcement text (end with Ctrl+D or Ctrl+Z on a new line):")
    
    try:
        lines = sys.stdin.readlines()
        if not lines:
            print("No input received.")
            return
        
        text = "".join(lines)
        print("\nParsing announcement...")
        
        result = parse_announcement(text)
        
        if not result["position"]:
            print("Error: Could not identify the position. Please check the format.")
            return
            
        print(f"Detected Position: {result['position']}")
        print(f"Detected Candidates: {len(result['candidates'])}")
        
        save_path = save_result(result)
        print(f"Data saved to: {save_path}")
        
        result_url = f"{BASE_URL}/results/{result['id']}"
        
        print("\n" + "*"*60)
        print("PUBLISHED SUCCESSFULLY!")
        print(f"Link: {result_url}")
        print("*"*60 + "\n")
        
    except EOFError:
        pass
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
