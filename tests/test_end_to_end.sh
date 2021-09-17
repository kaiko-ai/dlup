#!/bin/bash

# SSH to rhpc should be fixed
# Not sure from where this is run, and if DLUP is "already there" or if it needs to be loaded
# This should likely be called by some other script that gets the output from this script
#  and from there decides if the check succeeds or fails. So finally the if-else should return something?

SSH_USERNAME=githubactions
SSH_PATH_TO_LOCATION_OF_DIR=rhpc.nki.nl:/mnt/archive/
SSH_TAR_NAME=dlup_test_output

PATH_TO_WSI=TCGA-E2-A1B6-01Z-00-DX1.8CD458BE-C4F9-4AF3-A927-18C042E9B4B7.svs
REFERENCE_COMMIT_OUTPUT_DIR=output_from_commit_ebf9b00230feabf5da22236d14d53f0f60007c67
NEW_COMMIT_OUTPUT_DIR=output_from_new_commit
DLUP_PATH=dlup
PREV_USED_TILESIZE=1024
PREV_USED_TILEOVERLAP=0
PREV_USED_MPP=2

rsync -azv --progress=info2 $SSH_USERNAME@$SSH_PATH_TO_LOCATION_OF_DIR/$SSH_TAR_NAME.tar ./

tar -xvzf $SSH_TAR_NAME.tar

cd $DLUP_PATH

python setup.py develop

cd ..

dlup wsi tile $PATH_TO_WSI $NEW_COMMIT_OUTPUT_DIR \
--tile-size $PREV_USED_TILESIZE \
--tile-overlap $PREV_USED_TILEOVERLAP \
--mpp $PREV_USED_MPP

OUTPUT=$(echo $(git diff --no-info $SSH_TAR_NAME/$REFERENCE_COMMIT_OUTPUT_DIR $NEW_COMMIT_OUTPUT_DIR))

if [ "$(OUTPUT)" == "" ]; then
  echo "This is fine"
else
  echo "This is not fine. Check the differences: $(OUTPUT)"
fi

# if the output is empty, the directories are the same. if the output is anything,
# the directories are not the same, and it should not let the test pass, and show the output
