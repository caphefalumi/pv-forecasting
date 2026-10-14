"""Entry point for solar-evaluate ECS task definition (Table 20): python -m jobs.evaluate"""

from evaluate import main
import sys

if __name__ == "__main__":
    sys.exit(main())
