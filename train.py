# train.py
import os
import yaml
from ultralytics import YOLO

def main():
    # Fix data.yaml paths to absolute
    dataset_dir = os.path.join(os.getcwd(), "License-Plate-Recognition-4")
    yaml_path   = os.path.join(dataset_dir, "data.yaml")

    with open(yaml_path, "r") as f:
        data = yaml.safe_load(f)

    data["train"] = os.path.join(dataset_dir, "train", "images")
    data["val"]   = os.path.join(dataset_dir, "valid", "images")
    data["test"]  = os.path.join(dataset_dir, "test",  "images")

    fixed_yaml = os.path.join(dataset_dir, "data_fixed.yaml")
    with open(fixed_yaml, "w") as f:
        yaml.dump(data, f)

    print(f"Fixed yaml saved: {fixed_yaml}")

    # Load YOLO26 and train
    model = YOLO("yolo26n.pt")

    model.train(
        data    = fixed_yaml,
        epochs  = 50,
        imgsz   = 640,
        batch   = 32,
        device  = 0,
        project = "runs",
        name    = "lpr_yolo26",
        patience= 10,
        workers = 4,
        cache   = False,
    )

    print("Training complete!")
    print("Best model: runs/lpr_yolo26/weights/best.pt")

if __name__ == '__main__':
    main()