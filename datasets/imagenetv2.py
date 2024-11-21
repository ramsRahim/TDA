import os
from .utils import Datum, DatasetBase, listdir_nohidden

from collections import OrderedDict

template = ["itap of a {}.",
                        "a bad photo of the {}.",
                        "a origami {}.",
                        "a photo of the large {}.",
                        "a {} in a video game.",
                        "art of the {}.",
                        "a photo of the small {}."]

class ImageNetV2(DatasetBase):
    """ImageNetV2.

    This dataset is used for testing only.
    """

    dataset_dir = "ImageNetV2"

    def __init__(self, root):
        root = os.path.abspath(os.path.expanduser(root))
        self.dataset_dir = os.path.join(root, self.dataset_dir)
        image_dir = "imagenetv2-matched-frequency-format-val"
        self.image_dir = os.path.join(self.dataset_dir, image_dir)
        self.template = template
        text_file = os.path.join(self.dataset_dir, "classnames.txt")
        classnames = self.read_classnames(text_file)

        data = self.read_data(classnames)

        super().__init__(test=data) 

    def read_classnames(self,text_file):
        """Return a dictionary containing
        key-value pairs of <folder name>: <class name>.
        """
        classnames = OrderedDict()
        with open(text_file, "r") as f:
            lines = f.readlines()
            for line in lines:
                line = line.strip().split("\t")
                folder = line[0]
                classname = " ".join(line[1:])
                classnames[folder] = classname
        return classnames

    def read_data(self, classnames):
        image_dir = self.image_dir
        folders = list(classnames.keys())
        items = []

        for label in range(1000):
            class_dir = os.path.join(image_dir, str(label))
            imnames = listdir_nohidden(class_dir)
            folder = folders[label]
            classname = classnames[folder]
            for imname in imnames:
                impath = os.path.join(class_dir, imname)
                item = Datum(impath=impath, label=label, classname=classname)
                items.append(item)

        return items
