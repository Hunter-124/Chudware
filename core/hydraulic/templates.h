// core/hydraulic/templates.h — native starting circuits and suggested transient setups.
#pragma once

#include <string>

#include "hydraulic/schema.h"

namespace chudware::hydraulic {

// Catalog envelope: {ok:true, templates:[{id, name, description, components, hoses}]}.
ojson hydraulic_templates();
// hydraulic_new envelope, including template id and simulation setup (without doc).
ojson new_from_template(const std::string& name, const std::string& template_id = "blank");

}  // namespace chudware::hydraulic
