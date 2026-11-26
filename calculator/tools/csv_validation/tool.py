from csv_format import validate


def run(inputs, context):
    with context.open_input(inputs['upload_id']) as stream:
        return validate(stream)
