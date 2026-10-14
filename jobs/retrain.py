"""Entry point for solar-retrain ECS task definition (Table 20): python -m jobs.retrain"""

from retrain import main
import sys

if __name__ == "__main__":
    sys.exit(main())
