#include "doc/document.h"
#include "geom/dress.h"

int main(int argc, char** argv) {
    return geom::fillet_worker_main(argc, argv, &chudware::doc::validate_fillet_solid);
}
