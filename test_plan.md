1. **Define a configuration object**:
   - Create a `S7DirectAdapterConfig` dataclass in `src/aviapars/adapters/s7_direct.py` that encapsulates all the parameters currently passed to `S7DirectAdapter.__init__`.

2. **Refactor `S7DirectAdapter.__init__`**:
   - Update `S7DirectAdapter.__init__` to accept a single `config: S7DirectAdapterConfig | None = None` parameter.
   - Adjust the internal assignments to use `config`.

3. **Update usage in `__main__.py`**:
   - Modify the instantiation of `S7DirectAdapter` in `src/aviapars/__main__.py` to pass an instance of `S7DirectAdapterConfig`.

4. **Update tests**:
   - The test file `tests/adapters/test_s7_direct.py` initializes `S7DirectAdapter` without arguments, which will still work since the `config` argument is optional and defaults to `None`.

5. **Run Pre-Commit Checks & Tests**:
   - Ensure proper testing, verification, review, and reflection are done by calling the necessary pre-commit steps.

6. **Submit**:
   - Submit the refactoring changes.
