import os
import json
import random

# Create 10 folders named "1" through "10"
for i in range(1, 11):
    folder_name = str(i)
    os.makedirs(folder_name, exist_ok=True)
    
    # Inside each folder, create 100 JSON files
    for j in range(1, 101):
        data = {
            "numbers": [random.random() for _ in range(100)]  # 100 random floats in [0, 1)
        }
        file_path = os.path.join(folder_name, f"{j}.json")
        
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)

print("✅ Done! Created 10 folders, each with 100 JSON files.")
