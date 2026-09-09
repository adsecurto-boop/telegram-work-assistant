"""Safe test entry point; never reads production configuration or storage."""
import unittest
if __name__ == '__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests'))
    raise SystemExit(not result.wasSuccessful())
