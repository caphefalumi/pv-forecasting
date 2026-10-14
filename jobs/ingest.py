"""Entry point for solar-ingest ECS task definition (Table 20): python -m jobs.ingest"""

from ingest import main
import sys

if __name__ == "__main__":
    sys.exit(main())
