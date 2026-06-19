import json
import hmac
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Sum, Q
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse, Http404
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import AuthToken, Category, Product, Order, OrderItem, Payment


def _json_response(data, status=200):
    return JsonResponse(data, status=status)


def _parse_json(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return {}


def _verify_webhook_signature(request):
    timestamp = request.headers.get('X-Lux-Timestamp', '')
    signature = request.headers.get('X-Lux-Signature', '')

    if not timestamp or not signature:
        return False

    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False

    now_ts = int(datetime.utcnow().timestamp())
    if abs(now_ts - timestamp_int) > 300:
        return False

    payload = request.body.decode('utf-8')
    signed_payload = f'{timestamp}.{payload}'.encode('utf-8')
    expected = hmac.new(
        settings.POS_WEBHOOK_SECRET.encode('utf-8'),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _finalize_payment_internal(order, transaction_id, status, amount=None, method='pos_terminal', reference=''):
    status_map = {
        'approved': 'approved',
        'settled': 'settled',
        'failed': 'declined',
        'declined': 'declined',
        'cancelled': 'cancelled',
    }
    payment_status = status_map.get(status, 'declined')

    payment = getattr(order, 'payment', None)
    if payment is None:
        payment = Payment.objects.create(
            order=order,
            amount=amount if amount is not None else order.total,
            method=method,
            status=payment_status,
            transaction_id=transaction_id,
            reference=reference,
        )
    else:
        if amount is not None:
            payment.amount = amount
        payment.method = method or payment.method
        payment.status = payment_status
        payment.reference = reference or payment.reference
        if transaction_id:
            payment.transaction_id = transaction_id
        payment.save()

    if payment_status in ['approved', 'settled']:
        order.payment_status = 'paid'
        order.status = 'processing'
    else:
        order.payment_status = 'failed'
        order.status = 'cancelled'

    order.save()
    return payment


def _get_user_from_token(request):
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token_key = auth_header.split('Bearer ')[1].strip()
        try:
            token = AuthToken.objects.get(key=token_key)
            return token.user
        except AuthToken.DoesNotExist:
            return None
    return None


def home(request):
    return render(request, 'products/index.html')


def product_list(request):
    products = Product.objects.filter(is_active=True).select_related('category')
    data = [
        {
            'id': product.id,
            'name': product.name,
            'slug': product.slug,
            'description': product.description,
            'price': str(product.price),
            'category': product.category.name if product.category else None,
            'image_url': product.image_url,
            'is_active': product.is_active,
        }
        for product in products
    ]
    return JsonResponse({'products': data})


def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk, is_active=True)
    return JsonResponse(
        {
            'id': product.id,
            'name': product.name,
            'slug': product.slug,
            'description': product.description,
            'price': str(product.price),
            'category': product.category.name if product.category else None,
            'image_url': product.image_url,
            'is_active': product.is_active,
        }
    )


def category_list(request):
    categories = Category.objects.all()
    data = [{'id': category.id, 'name': category.name, 'slug': category.slug} for category in categories]
    return JsonResponse({'categories': data})


@csrf_exempt
@require_http_methods(['POST'])
def register(request):
    payload = _parse_json(request)
    email = payload.get('email')
    name = payload.get('name')
    password = payload.get('password')

    if not email or not password or not name:
        return _json_response({'error': 'Email, name, and password are required.'}, status=400)

    if User.objects.filter(email=email).exists():
        return _json_response({'error': 'A user with that email already exists.'}, status=400)

    user = User.objects.create_user(username=email, email=email, password=password, first_name=name)
    token = AuthToken.objects.create(user=user)

    return _json_response({
        'token': token.key,
        'user': {'name': user.first_name, 'email': user.email},
        'message': 'Registration successful.',
    }, status=201)


@csrf_exempt
@require_http_methods(['POST'])
def login_view(request):
    payload = _parse_json(request)
    email = payload.get('email')
    password = payload.get('password')

    if not email or not password:
        return _json_response({'error': 'Email and password are required.'}, status=400)

    user = authenticate(request, username=email, password=password)
    if user is None:
        return _json_response({'error': 'Invalid credentials.'}, status=401)

    token, _ = AuthToken.objects.get_or_create(user=user)
    return _json_response({
        'token': token.key,
        'user': {'name': user.first_name or user.username, 'email': user.email},
        'message': 'Login successful.',
    })


@csrf_exempt
@require_http_methods(['POST'])
def logout_view(request):
    user = _get_user_from_token(request)
    if not user:
        return _json_response({'error': 'Invalid token.'}, status=401)

    AuthToken.objects.filter(user=user).delete()
    return _json_response({'message': 'Successfully logged out.'})


@csrf_exempt
@require_http_methods(['POST'])
def create_order(request):
    payload = _parse_json(request)
    
    customer_name = payload.get('customer_name')
    customer_email = payload.get('customer_email')
    customer_phone = payload.get('customer_phone')
    customer_address = payload.get('customer_address')
    payment_method = payload.get('payment_method', 'pos_terminal')
    items = payload.get('items', [])

    if not all([customer_name, customer_email, customer_phone, customer_address, items]):
        return _json_response(
            {'error': 'Missing required fields.'},
            status=400
        )

    try:
        order = Order.objects.create(
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            customer_address=customer_address,
            payment_method=payment_method,
            status='pending',
        )

        total_amount = 0
        for item in items:
            product_id = item.get('product_id')
            quantity = item.get('quantity', 1)
            price = item.get('price', 0)

            try:
                product = Product.objects.get(id=product_id)
            except Product.DoesNotExist:
                return _json_response(
                    {'error': f'Product {product_id} not found.'},
                    status=400
                )

            order_item = OrderItem.objects.create(
                order=order,
                product=product,
                product_name=product.name,
                price=price,
                quantity=quantity,
            )
            total_amount += order_item.subtotal

        order.subtotal = total_amount
        order.total = total_amount
        order.save()

        return _json_response({
            'order_id': order.id,
            'order_number': order.order_number,
            'total': str(order.total),
            'status': order.status,
            'items': [
                {
                    'product_name': item.product_name,
                    'quantity': item.quantity,
                    'price': str(item.price),
                    'subtotal': str(item.subtotal),
                }
                for item in order.items.all()
            ],
        }, status=201)

    except Exception as e:
        return _json_response({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(['POST'])
def process_pos_payment(request):
    """Process payment through POS terminal."""
    payload = _parse_json(request)
    
    order_id = payload.get('order_id')
    amount = payload.get('amount')
    method = payload.get('method', 'pos_terminal')
    reference = payload.get('reference', '')
    account_name = payload.get('account_name', '')
    account_number = payload.get('account_number', '')

    if account_name and account_number:
        reference = f"{reference}|SETTLE:{account_name}:{account_number}"[:100]

    if not order_id or not amount:
        return _json_response(
            {'error': 'order_id and amount are required.'},
            status=400
        )

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return _json_response({'error': 'Order not found.'}, status=404)

    try:
        import secrets
        transaction_id = f'TXN-{secrets.token_hex(8).upper()}'
        payment = _finalize_payment_internal(
            order=order,
            transaction_id=transaction_id,
            status='approved',
            amount=amount,
            method=method,
            reference=reference,
        )

        return _json_response({
            'payment_id': payment.id,
            'transaction_id': transaction_id,
            'order_number': order.order_number,
            'amount': str(payment.amount),
            'status': payment.status,
            'reference': payment.reference,
            'settlement_account_name': account_name,
            'settlement_account_number': account_number,
            'message': f'Payment approved. Order {order.order_number} is being processed.',
        }, status=200)

    except Exception as e:
        return _json_response({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(['POST'])
def finalize_payment(request):
    auth_token = request.headers.get('X-Internal-Token', '')
    if auth_token != settings.POS_INTERNAL_API_TOKEN:
        return _json_response({'error': 'Unauthorized finalization request.'}, status=401)

    payload = _parse_json(request)
    order_id = payload.get('order_id')
    transaction_id = payload.get('transaction_id', '')
    status = payload.get('status', 'approved').lower()
    amount_raw = payload.get('amount')
    method = payload.get('method', 'pos_terminal')
    reference = payload.get('reference', '')

    if not order_id or not transaction_id:
        return _json_response({'error': 'order_id and transaction_id are required.'}, status=400)

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return _json_response({'error': 'Order not found.'}, status=404)

    amount = None
    if amount_raw is not None:
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, ValueError):
            return _json_response({'error': 'Invalid amount supplied.'}, status=400)

    try:
        payment = _finalize_payment_internal(
            order=order,
            transaction_id=transaction_id,
            status=status,
            amount=amount,
            method=method,
            reference=reference,
        )
        return _json_response({
            'message': f'Payment finalized for order {order.order_number}.',
            'order_number': order.order_number,
            'payment_status': payment.status,
            'transaction_id': payment.transaction_id,
        })
    except Exception as e:
        return _json_response({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(['POST'])
def payment_webhook(request):
    if not _verify_webhook_signature(request):
        return _json_response({'error': 'Invalid webhook signature.'}, status=401)

    payload = _parse_json(request)
    event_id = payload.get('event_id')
    event_type = payload.get('event_type')
    order_id = payload.get('order_id')
    transaction_id = payload.get('transaction_id', '')
    amount_raw = payload.get('amount')
    method = payload.get('method', 'pos_terminal')
    provider_reference = payload.get('provider_reference', '')

    if not event_id or not event_type:
        return _json_response({'error': 'event_id and event_type are required.'}, status=400)

    idempotency_key = f'webhook_event:{event_id}'
    if cache.get(idempotency_key):
        return _json_response({'message': 'Event already processed.', 'event_id': event_id})

    if not order_id or not transaction_id:
        return _json_response({'error': 'order_id and transaction_id are required.'}, status=400)

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return _json_response({'error': 'Order not found.'}, status=404)

    status_by_event = {
        'payment.approved': 'approved',
        'payment.settled': 'settled',
        'payment.failed': 'failed',
        'payment.declined': 'declined',
        'payment.cancelled': 'cancelled',
    }
    mapped_status = status_by_event.get(event_type)
    if mapped_status is None:
        return _json_response({'error': f'Unsupported event_type: {event_type}'}, status=400)

    amount = None
    if amount_raw is not None:
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, ValueError):
            return _json_response({'error': 'Invalid amount supplied.'}, status=400)

    try:
        payment = _finalize_payment_internal(
            order=order,
            transaction_id=transaction_id,
            status=mapped_status,
            amount=amount,
            method=method,
            reference=provider_reference,
        )
        cache.set(idempotency_key, True, timeout=60 * 60 * 24)
        return _json_response({
            'message': 'Webhook processed successfully.',
            'event_id': event_id,
            'order_number': order.order_number,
            'payment_status': payment.status,
        })
    except Exception as e:
        return _json_response({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(['GET'])
def order_detail(request, order_id):
    """Get order details including items and payment status."""
    try:
        order = Order.objects.get(id=order_id)
        payment = order.payment if hasattr(order, 'payment') else None

        return _json_response({
            'order_id': order.id,
            'order_number': order.order_number,
            'customer_name': order.customer_name,
            'customer_email': order.customer_email,
            'total': str(order.total),
            'status': order.status,
            'payment_status': order.payment_status,
            'payment_method': order.payment_method,
            'created_at': order.created_at.isoformat(),
            'items': [
                {
                    'product_name': item.product_name,
                    'quantity': item.quantity,
                    'price': str(item.price),
                    'subtotal': str(item.subtotal),
                }
                for item in order.items.all()
            ],
            'payment': {
                'transaction_id': payment.transaction_id,
                'status': payment.status,
                'amount': str(payment.amount),
            } if payment else None,
        })

    except Order.DoesNotExist:
        return _json_response({'error': 'Order not found.'}, status=404)
    except Exception as e:
        return _json_response({'error': str(e)}, status=500)


@login_required
def staff_dashboard(request):
    products = Product.objects.all().select_related('category')
    categories = Category.objects.all()
    return render(request, 'products/dashboard.html', {'products': products, 'categories': categories})


# ============== ADMIN DASHBOARD ENDPOINTS ==============

@csrf_exempt
@require_http_methods(['GET', 'POST', 'PUT', 'DELETE'])
def admin_products(request):
    """Manage products - list, create, update, delete."""
    if request.method == 'GET':
        products = Product.objects.all().select_related('category')
        data = [
            {
                'id': p.id,
                'name': p.name,
                'price': str(p.price),
                'stock': p.stock_quantity,
                'category': p.category.name if p.category else None,
                'is_active': p.is_active,
            }
            for p in products
        ]
        return _json_response({'products': data})

    elif request.method == 'POST':
        payload = _parse_json(request)
        try:
            category_id = payload.get('category_id')
            category = None
            if category_id:
                category = Category.objects.get(id=category_id)

            product = Product.objects.create(
                name=payload.get('name'),
                description=payload.get('description', ''),
                price=payload.get('price'),
                category=category,
                image_url=payload.get('image_url', ''),
                stock_quantity=payload.get('stock', 0),
                is_active=payload.get('is_active', True),
            )
            return _json_response({
                'id': product.id,
                'name': product.name,
                'message': 'Product created successfully.',
            }, status=201)
        except Exception as e:
            return _json_response({'error': str(e)}, status=400)

    elif request.method == 'PUT':
        payload = _parse_json(request)
        product_id = payload.get('id')
        try:
            product = Product.objects.get(id=product_id)
            product.name = payload.get('name', product.name)
            product.description = payload.get('description', product.description)
            product.price = payload.get('price', product.price)
            product.stock_quantity = payload.get('stock', product.stock_quantity)
            product.is_active = payload.get('is_active', product.is_active)
            product.save()
            return _json_response({'id': product.id, 'message': 'Product updated.'})
        except Product.DoesNotExist:
            return _json_response({'error': 'Product not found.'}, status=404)

    elif request.method == 'DELETE':
        payload = _parse_json(request)
        product_id = payload.get('id')
        try:
            product = Product.objects.get(id=product_id)
            product.delete()
            return _json_response({'message': 'Product deleted.'})
        except Product.DoesNotExist:
            return _json_response({'error': 'Product not found.'}, status=404)


@csrf_exempt
@require_http_methods(['GET'])
def admin_orders(request):
    """Get orders with optional filtering by status."""
    status_filter = request.GET.get('status')
    
    orders = Order.objects.all().prefetch_related('items', 'payment')
    if status_filter:
        orders = orders.filter(payment_status=status_filter)

    data = [
        {
            'id': o.id,
            'order_number': o.order_number,
            'customer_name': o.customer_name,
            'customer_email': o.customer_email,
            'total': str(o.total),
            'status': o.status,
            'payment_status': o.payment_status,
            'created_at': o.created_at.isoformat(),
            'item_count': o.items.count(),
        }
        for o in orders
    ]
    return _json_response({'orders': data})


@csrf_exempt
@require_http_methods(['GET'])
def admin_payments(request):
    """Get payment history and totals."""
    payments = Payment.objects.all().select_related('order')
    
    # Group by status
    stats = {
        'total_approved': payments.filter(status='approved').aggregate(Sum('amount'))['amount__sum'] or 0,
        'total_settled': payments.filter(status='settled').aggregate(Sum('amount'))['amount__sum'] or 0,
        'total_pending': payments.filter(status='pending').aggregate(Sum('amount'))['amount__sum'] or 0,
        'total_declined': payments.filter(status='declined').aggregate(Sum('amount'))['amount__sum'] or 0,
        'transaction_count': payments.count(),
    }

    data = [
        {
            'id': p.id,
            'order_number': p.order.order_number,
            'transaction_id': p.transaction_id,
            'amount': str(p.amount),
            'method': p.method,
            'status': p.status,
            'created_at': p.created_at.isoformat(),
        }
        for p in payments
    ]
    return _json_response({
        'payments': data,
        'stats': {
            'total_approved': str(stats['total_approved']),
            'total_settled': str(stats['total_settled']),
            'total_pending': str(stats['total_pending']),
            'total_declined': str(stats['total_declined']),
            'transaction_count': stats['transaction_count'],
        }
    })


@csrf_exempt
@require_http_methods(['GET'])
def admin_reports(request):
    """Get sales reports - daily, weekly, monthly."""
    report_type = request.GET.get('type', 'daily')  # daily, weekly, monthly
    
    paid_orders = Order.objects.filter(payment_status='paid')
    
    if report_type == 'daily':
        today = datetime.now().date()
        data = paid_orders.filter(created_at__date=today)
    elif report_type == 'weekly':
        week_ago = datetime.now() - timedelta(days=7)
        data = paid_orders.filter(created_at__gte=week_ago)
    elif report_type == 'monthly':
        month_ago = datetime.now() - timedelta(days=30)
        data = paid_orders.filter(created_at__gte=month_ago)
    else:
        data = paid_orders

    total_sales = data.aggregate(Sum('total'))['total__sum'] or 0
    order_count = data.count()
    avg_order_value = float(total_sales) / order_count if order_count > 0 else 0

    return _json_response({
        'report_type': report_type,
        'total_sales': str(total_sales),
        'order_count': order_count,
        'average_order_value': f'{avg_order_value:.2f}',
        'orders': [
            {
                'order_number': o.order_number,
                'total': str(o.total),
                'created_at': o.created_at.isoformat(),
            }
            for o in data
        ],
    })


@csrf_exempt
@require_http_methods(['GET'])
def admin_reconciliation(request):
    target_date = request.GET.get('date')
    if target_date:
        try:
            date_value = datetime.strptime(target_date, '%Y-%m-%d').date()
        except ValueError:
            return _json_response({'error': 'date must be in YYYY-MM-DD format.'}, status=400)
    else:
        date_value = datetime.utcnow().date()

    day_payments = Payment.objects.filter(created_at__date=date_value).select_related('order')
    approved_total = day_payments.filter(status__in=['approved', 'settled']).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    settled_total = day_payments.filter(status='settled').aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    variance = approved_total - settled_total

    unsettled = day_payments.filter(status='approved')
    unsettled_data = [
        {
            'order_number': p.order.order_number,
            'transaction_id': p.transaction_id,
            'amount': str(p.amount),
            'status': p.status,
            'reference': p.reference,
            'created_at': p.created_at.isoformat(),
        }
        for p in unsettled
    ]

    return _json_response({
        'date': date_value.isoformat(),
        'approved_total': str(approved_total),
        'settled_total': str(settled_total),
        'variance': str(variance),
        'unsettled_count': len(unsettled_data),
        'unsettled_payments': unsettled_data,
    })
