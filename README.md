# LuxuryShop Django Backend

This Django backend provides a secure admin site and a product database API for the LuxuryShop app.

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run migrations:
   ```bash
   python manage.py migrate
   ```
4. Create a superuser for admin access:
   ```bash
   python manage.py createsuperuser
   ```
5. Start the development server:
   ```bash
   python manage.py runserver
   ```

## Access

- Admin site: `/admin/`
- Custom admin dashboard: `/admin/dashboard/`
- Product API: `/api/products/`
- Product detail API: `/api/products/<id>/`
- Category API: `/api/categories/`

## Notes

- Use the Django admin to manage categories and products.
- The custom dashboard page requires login and is styled for the LuxuryShop theme.
