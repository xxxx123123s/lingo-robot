#!/bin/bash
# 从 hf-mirror 下载 sherpa-onnx 中文 TTS 模型（vits-melo-tts-zh_en，int8）
set -e
BASE="https://hf-mirror.com/csukuangfj/vits-melo-tts-zh_en/resolve/main"
D="/home/pi/vits-melo-tts-zh_en"
mkdir -p "$D/dict/pos_dict"

FILES="model.int8.onnx lexicon.txt tokens.txt date.fst number.fst phone.fst \
dict/hmm_model.utf8 dict/idf.utf8 dict/jieba.dict.utf8 dict/stop_words.utf8 dict/user.dict.utf8 \
dict/pos_dict/char_state_tab.utf8 dict/pos_dict/prob_emit.utf8 dict/pos_dict/prob_start.utf8 dict/pos_dict/prob_trans.utf8"

for f in $FILES; do
  if [ -s "$D/$f" ]; then echo "跳过 $f"; continue; fi
  echo "下载 $f"
  curl -sL --retry 3 -o "$D/$f" "$BASE/$f"
done

echo "--- 完成 ---"
du -sh "$D"
ls -1 "$D"
