with payments as (

    select
        order_id,
        sum(amount) / 100 as amount
    from {{ ref('raw_payments') }}
    group by order_id

),

orders as (

    select
        id as order_id,
        user_id as customer_id
    from {{ ref('raw_orders') }}

)

select
    orders.customer_id,
    count(distinct orders.order_id) as orders,
    sum(payments.amount) as ltv
from orders
left join payments
    on orders.order_id = payments.order_id
group by orders.customer_id
