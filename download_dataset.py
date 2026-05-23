from roboflow import Roboflow

rf = Roboflow(api_key="nQbjDhcPS9lzLYxxtwAU")
project = rf.workspace("roboflow-universe-projects").project("license-plate-recognition-rxg4e")
version = project.version(4)
dataset = version.download("yolov8")

print("Dataset downloaded!")
print(f"Location: {dataset.location}")