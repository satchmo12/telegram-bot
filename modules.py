from customer.customer_qa import register_customer_qa_handlers
from customer.editUserInfo import register_customer_edit_handlers
from forward.message_forward import register_message_forward_handlers
from group.favorite import register_favorite
from group.note import register_note
from multi_bot_manager import register_multi_bot_manager_handlers
from registries.economy_registry import register_economy_handlers_group
from registries.entertainment_registry import register_entertainment_handlers
from registries.group_registry import register_group_handlers
from registries.simulation_registry import register_simulation_handlers

def register_all_handlers(app):
    register_multi_bot_manager_handlers(app)
    register_economy_handlers_group(app)
    register_entertainment_handlers(app)
    register_simulation_handlers(app)
    register_group_handlers(app)
    register_customer_qa_handlers(app)
    register_customer_edit_handlers(app)
    register_message_forward_handlers(app)
    register_note(app)
    register_favorite(app)
