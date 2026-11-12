#!/usr/bin/env bash
# Downloads the one model that is too large to ship with the project (SFace, 37 MB)
# from the official OpenCV Zoo and verifies its checksum.
set -euo pipefail
cd "$(dirname "$0")/../models"
FILE=face_recognition_sface_2021dec.onnx
SHA=0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
URL=https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/$FILE
if [ ! -f "$FILE" ]; then
  curl -L --fail -o "$FILE.part" "$URL"
  mv "$FILE.part" "$FILE"
fi
echo "$SHA  $FILE" | sha256sum -c -
