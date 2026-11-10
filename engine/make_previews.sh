#!/usr/bin/env bash
# Builds low-res previews from out/ and force-pushes them to the "previews" branch.
set +e
mkdir -p /tmp/prev
for d in out/*/; do
  c=$(basename $d)
  [ -f $d/long.mp4 ] && ffmpeg -loglevel error -y -i $d/long.mp4 -vf scale=-2:480 -c:v libx264 -crf 30 -preset veryfast -c:a aac -b:a 64k /tmp/prev/$c-long.mp4
  [ -f $d/short.mp4 ] && ffmpeg -loglevel error -y -i $d/short.mp4 -vf scale=-2:640 -c:v libx264 -crf 30 -preset veryfast -c:a aac -b:a 64k /tmp/prev/$c-short.mp4
  [ -f $d/thumb.jpg ] && cp $d/thumb.jpg /tmp/prev/$c-thumb.jpg
  [ -f $d/script.json ] && cp $d/script.json /tmp/prev/$c-script.json
  ls $d/img/*.jpg >/dev/null 2>&1 && python -c "
import glob,sys; from PIL import Image
fs=sorted(glob.glob('$d/img/*.jpg')); ims=[Image.open(f).convert('RGB') for f in fs]
w=320; ims=[i.resize((w,int(w*i.height/i.width))) for i in ims]; cols=6; rows=(len(ims)+cols-1)//cols; h=max(i.height for i in ims)
c=Image.new('RGB',(cols*w,rows*h)); [c.paste(im,((k%cols)*w,(k//cols)*h)) for k,im in enumerate(ims)]; c.save('/tmp/prev/$c-images.jpg',quality=80)"
done
ls /tmp/prev | grep -q . || exit 0
cd /tmp/prev && git init -q -b previews && git add -A && git -c user.name=padhan-video-engine -c user.email=actions@users.noreply.github.com commit -qm "previews $(date -u +%F)"
git push -fq "https://x-access-token:${GH_TOKEN}@github.com/${GH_REPO}" previews
