import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)                                                    # domain, agent, ports, adapters, handlers
sys.path.append(os.path.join(here, "..", "synthetics"))                     # safety.py (copied into the bundle)
