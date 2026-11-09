Resolve this GitHub issue in the `django/django` repository, which is checked out in /workspace at the commit the issue was filed against (the package is installed in editable mode).

Change the library source so the issue is resolved, in the way a maintainer would accept. Hidden tests written for the fix will be run after you finish, together with the existing test suite. Don't edit or add tests.

<issue>
make temporal subtraction work without ExpressionWrapper
Description
	
class Experiment(models.Model):
	start = models.DateTimeField()
	end = models.DateTimeField()
Experiment.objects.annotate(
	delta=F('end') - F('start') + Value(datetime.timedelta(), output_field=DurationField())
)
This gives:
django.core.exceptions.FieldError: Expression contains mixed types: DateTimeField, DurationField. You must set output_field.
</issue>
