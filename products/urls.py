from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('api/products/', views.product_list, name='product_list'),
    path('api/products/<int:pk>/', views.product_detail, name='product_detail'),
    path('api/categories/', views.category_list, name='category_list'),
    path('api/register/', views.register, name='api_register'),
    path('api/login/', views.login_view, name='api_login'),
    path('api/logout/', views.logout_view, name='api_logout'),
    path('api/orders/', views.create_order, name='api_create_order'),
    path('api/orders/<int:order_id>/', views.order_detail, name='api_order_detail'),
    path('api/payments/', views.process_pos_payment, name='api_process_payment'),
    path('api/payments/finalize/', views.finalize_payment, name='api_finalize_payment'),
    path('api/payments/webhook/', views.payment_webhook, name='api_payment_webhook'),
    # Admin endpoints
    path('api/admin/products/', views.admin_products, name='api_admin_products'),
    path('api/admin/orders/', views.admin_orders, name='api_admin_orders'),
    path('api/admin/payments/', views.admin_payments, name='api_admin_payments'),
    path('api/admin/reports/', views.admin_reports, name='api_admin_reports'),
    path('api/admin/reconciliation/', views.admin_reconciliation, name='api_admin_reconciliation'),
    path('admin/dashboard/', views.staff_dashboard, name='staff_dashboard'),
]
