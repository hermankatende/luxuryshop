from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0004_product_stock_quantity'),
    ]

    operations = [
        migrations.AlterField(
            model_name='payment',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('approved', 'Approved'),
                    ('settled', 'Settled'),
                    ('declined', 'Declined'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
