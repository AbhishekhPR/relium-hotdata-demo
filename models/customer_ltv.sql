with payments as (

    select
        order_id,
        amount / 100 as amount
    from {{ ref('raw_payments') }}

),

orders as (

    select
        id as order_id,
        user_id as customer_id
    from {{ ref('raw_orders') }}

)

select
    orders.customer_id,
    count(orders.order_id) as orders,
    sum(payments.amount) as ltv
from orders
left join payments
    on orders.order_id = payments.order_id
group by orders.customer_id
