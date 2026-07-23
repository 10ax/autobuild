# autobuild/__main__.py
import sys
from autobuild.daemon import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
