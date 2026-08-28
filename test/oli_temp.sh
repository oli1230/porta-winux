dd if=/dev/urandom of=/tmp/test.bin bs=1M count=4096
sha256sum /tmp/test.bin
cp /tmp/test.bin /run/media/obs1230/OliDrive/ && sync
echo 3 | sudo tee /proc/sys/vm/drop_caches   # force a real read
sha256sum /run/media/obs1230/OliDrive/test.bin