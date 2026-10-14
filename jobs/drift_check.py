"""Entry point for solar-drift ECS task definition (Table 20): python -m jobs.drift_check"""

from drift import main
import sys

if __name__ == "__main__":
    sys.exit(main())
