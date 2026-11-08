Resolve this GitHub issue in the `pallets/flask` repository, which is checked out in /workspace at the commit the issue was filed against (the package is installed in editable mode).

Change the library source so the issue is resolved, in the way a maintainer would accept. Hidden tests written for the fix will be run after you finish, together with the existing test suite. Don't edit or add tests.

<issue>
Raise error when blueprint name contains a dot
This is required since every dot is now significant since blueprints can be nested. An error was already added for endpoint names in 1.0, but should have been added for this as well.
</issue>
