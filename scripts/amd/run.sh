set -e

# sh scripts/amd/build_pytorch_jenkins.sh
sh scripts/amd/build_pytorch_develop.sh

sh scripts/amd/test_spectral.sh