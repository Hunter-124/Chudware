#include "firmware/bus_models.h"
#include <cmath>
#include <iomanip>
#include <set>
#include <sstream>
#include <stdexcept>
#include "ecad/impedance.h"
#include "sim/spice.h"
#include <regex>
#include "firmware_bus_sources.h"

namespace chudware::firmware {
namespace {
using json = nlohmann::json;
const std::map<std::string, std::set<std::string>> signals = {
    {"i2c", {"scl", "sda"}}, {"spi", {"sck", "mosi", "miso", "cs"}},
    {"uart", {"tx", "rx"}}, {"can", {"can_diff"}}};
bool number(const json& j, const char* key, bool positive) {
    return j.contains(key) && j[key].is_number() && std::isfinite(j[key].get<double>()) &&
           (positive ? j[key].get<double>() > 0 : j[key].get<double>() >= 0);
}
std::string text(const json& j, const char* key) {
    return j.contains(key) && j[key].is_string() ? j[key].get<std::string>() : "";
}
void resolve_segment(json& segment) {
    if(!segment.is_object() || !segment.contains("ecad_source")) return;
    const auto& source=segment.at("ecad_source");
    const auto& board=source.at("board");
    const auto& selector=source.at("track_or_net");
    json selected=selector.is_object()?selector:json();
    if(selector.is_string() && board.contains("tracks") && board["tracks"].is_array())
        for(const auto& track:board["tracks"])
            if(track.is_object() && track.value("id",json())==selector) {selected=track;break;}
    // The geometry helper aggregates net-tagged tracks. Isolate only its input
    // copy, retaining the original board/selector and net provenance unchanged.
    json reference;
    if(selected.is_object()) {
        const auto net=selected.value("net",json());
        selected.erase("net");
        reference=ecad::track_impedance(board,selected);
        reference["net"]=net;
    } else reference=ecad::track_impedance(board,selector);
    if(!segment.contains("z0_ohm")) segment["z0_ohm"]=reference.at("z0");
    if(!segment.contains("propagation_s")) {
        if(source.contains("length_mm")) {
            if(!number(source,"length_mm",true)) throw std::invalid_argument("ECAD segment length_mm must be finite positive");
            segment["propagation_s"]=reference.at("delay_ps_per_mm").get<double>()*source.at("length_mm").get<double>()*1e-12;
        } else {
            if(!selected.is_object()) throw std::invalid_argument("branched/net ECAD selection requires explicit physical segment length_mm or propagation_s");
            segment["propagation_s"]=reference.at("delay_ps").get<double>()*1e-12;
        }
    }
    segment["ecad_reference"]=reference;
}
std::string literal(const json& v) {
    if(v.is_object()) {
        std::string out="new Dictionary<string, object> {";
        for(const auto& [key,value]:v.items()) out+="{"+json(key).dump()+","+literal(value)+"},";
        return out+"}";
    }
    if(v.is_array()) { std::string out="new object[] {";for(const auto& value:v) out+=literal(value)+",";return out+"}"; }
    if(v.is_null()) return "null";
    if(v.is_string() || v.is_boolean()) return v.dump();
    std::ostringstream out;out<<std::setprecision(17)<<v.get<double>()<<"d";return out.str();
}
}

std::vector<std::string> validate_bus_links(const json& sc) {
    std::vector<std::string> errors;
    if(!sc.is_object()) return errors;
    if(!sc.contains("bus_links")) {
        if(sc.contains("tracks") && sc["tracks"].is_array()) for(const auto& track:sc["tracks"])
            if(track.is_object() && track.contains("link_id")) errors.push_back("track link_id requires bus_links declaration");
        return errors;
    }
    if(!sc["bus_links"].is_array()) return {"bus_links must be an array"};
    std::set<std::string> ids,buses;
    for(const auto& link:sc["bus_links"]) {
        if(!link.is_object()) {errors.push_back("bus_links entries must be objects");continue;}
        std::string id=text(link,"id"), bus=text(link,"bus"), protocol=text(link,"protocol");
        std::string tag="bus_link '"+id+"': ";
        auto fail=[&](const std::string& message){errors.push_back(tag+message);};
        if(id.empty() || !ids.insert(id).second) fail("id must be unique and nonempty");
        if(bus.empty() || !buses.insert(bus).second) fail("bus must be nonempty and owned by exactly one link");
        if(!std::regex_match(bus,std::regex("^[a-z][a-z0-9]*$"))) fail("bus must be a monitor-safe peripheral identifier");
        for(const char* key:{"limitations","sources"}) if(link.contains(key) && !link[key].is_array())
            fail(std::string(key)+" must be an array");
        if(!signals.count(protocol)) {fail("protocol must be i2c/spi/uart/can");continue;}
        if(!number(link,"controller_clock_hz",true)) fail("controller_clock_hz must be explicit finite positive Hz (the actual peripheral clock)");
        if(!link.contains("peer") || !link["peer"].is_object()) {fail("peer must be an explicit object");continue;}
        const auto& peer=link["peer"];
        for(const char* key:{"response_delay_s","clock_stretch_s","clock_to_output_s","setup_s","hold_s"}) if(peer.contains(key)&&!number(peer,key,false)) fail(std::string("peer.")+key+" must be finite nonnegative seconds");
        if(protocol=="uart") {
            if(!number(peer,"baud_hz",true)) fail("UART peer baud_hz is required");
            if(text(peer,"electrical_variant")!="ttl") fail("only explicit UART electrical_variant=ttl is supported");
        }
        if(protocol=="can" && !number(peer,"bitrate_hz",true)) fail("CAN peer bitrate_hz is required");
        if(protocol=="spi" && (text(peer,"controller_mode")=="slave" || text(peer,"variant")=="i2s" || text(peer,"variant")=="one_wire"))
            fail("SPI slave controller/I2S/one-wire variants are unsupported");
        if(protocol=="spi" && peer.contains("cs_gpio") && (!std::regex_match(text(peer,"cs_gpio"),std::regex("^gpioPort[A-K]$")) ||
           !peer.contains("cs_pin") || !peer["cs_pin"].is_number_integer() || peer["cs_pin"].get<int>()<0 || peer["cs_pin"].get<int>()>15))
            fail("SPI peer CS requires cs_gpio=gpioPortA..K and cs_pin=0..15");
        if(!link.contains("electrical") || !link["electrical"].is_object() || !link["electrical"].contains("signals") || !link["electrical"]["signals"].is_array()) {fail("electrical.signals array is required; omit bus_links for functional mode");continue;}
        std::set<std::string> declared;
        for(const auto& signal:link["electrical"]["signals"]) {
            if(!signal.is_object()) {fail("signals entries must be objects");continue;}
            std::string name=text(signal,"name"), model=text(signal,"model");
            if(!signals.at(protocol).count(name) || !declared.insert(name).second) fail("unknown/duplicate signal '"+name+"'");
            if(text(signal,"mode")!=(protocol=="can"?"differential":"single_ended")) fail("signal mode does not match protocol");
            std::set<std::string> locations,endpoints;
            if(!signal.contains("endpoints") || !signal["endpoints"].is_array() || signal["endpoints"].size()<2) {fail(name+" requires at least mcu and peer endpoints");continue;}
            for(const auto& endpoint:signal["endpoints"]) {
                if(!endpoint.is_object()) {fail("endpoint must be an object");continue;}
                if(text(endpoint,"id").empty() || !endpoints.insert(text(endpoint,"id")).second) fail("endpoint ids must be unique nonempty");
                if(text(endpoint,"at").empty()) fail("endpoint at is required");else locations.insert(text(endpoint,"at"));
                for(const char* key:{"input_capacitance_f","rise_s","fall_s","tx_delay_s","rx_delay_s"}) if(!number(endpoint,key,false)) fail(name+" endpoint "+key+" must be explicit finite nonnegative SI value");
                for(const char* key:{"r_low_ohm","r_high_ohm"}) if(!number(endpoint,key,true)) fail(name+" driver "+key+" must be explicit finite positive ohms");
                for(const char* key:{"v_low_v","v_high_v"}) if(!number(endpoint,key,false)) fail(name+" driver "+key+" must be explicit finite nonnegative volts");
                if(protocol=="can") {
                    const bool external=text(endpoint,"role")=="external_driver" && text(endpoint,"id")!="mcu" && text(endpoint,"id")!="peer";
                    if((text(endpoint,"drive")!="can_dominant" && !(external && text(endpoint,"drive")=="push_pull")) ||
                       !number(endpoint,"recessive_max_v",false) || !number(endpoint,"dominant_min_v",true))
                        fail("CAN receivers need can_dominant; explicit external_driver may push_pull; differential thresholds required");
                    else if(endpoint["recessive_max_v"].get<double>()>=endpoint["dominant_min_v"].get<double>()) fail("CAN thresholds must be ordered recessive_max_v < dominant_min_v");
                } else {
                    if(text(endpoint,"drive")!=(protocol=="i2c"?"open_drain":"push_pull")) fail(name+" driver type does not match protocol");
                    if(!number(endpoint,"vil_v",false) || !number(endpoint,"vih_v",true)) fail(name+" receiver vil_v/vih_v are required");
                    else if(endpoint["vil_v"].get<double>()>=endpoint["vih_v"].get<double>()) fail("thresholds must be ordered vil_v < vih_v");
                }
            }
            if(!endpoints.count("mcu") || !endpoints.count("peer")) fail(name+" requires endpoints mcu and peer");
            if(signal.contains("segments") && !signal["segments"].is_array())
                fail("segments must be an array");
            if(model=="lumped_rc") {
                if(!number(signal,"series_r_ohm",false) || !number(signal,"wire_capacitance_f",false)) fail(name+" lumped_rc requires explicit series_r_ohm/wire_capacitance_f");
                if(signal.contains("segments") && !signal["segments"].empty()) fail("lumped_rc cannot also have distributed segments");
            } else if(model=="lossless_tline") {
                if(signal.contains("wire_capacitance_f") && (!number(signal,"wire_capacitance_f",false)||signal["wire_capacitance_f"].get<double>()!=0)) fail("lossless line distributed capacitance cannot be counted again");
                if(!signal.contains("segments") || !signal["segments"].is_array() || signal["segments"].empty()) fail("lossless_tline requires segments");
                else for(auto segment:signal["segments"]) {
                    try {resolve_segment(segment);} catch(const std::exception& error) {fail(std::string("invalid ECAD segment source: ")+error.what());}
                    if(!segment.is_object() || !number(segment,"z0_ohm",true) || !number(segment,"propagation_s",true) || text(segment,"from").empty() || text(segment,"to").empty() || text(segment,"from")==text(segment,"to")) fail("segments require distinct from/to and finite positive z0_ohm/propagation_s");
                    else {locations.insert(text(segment,"from"));locations.insert(text(segment,"to"));}
                }
            } else fail("signal model must be lumped_rc or lossless_tline");
            for(const char* key:{"bias","terminations"}) if(signal.contains(key)) {
                if(!signal[key].is_array()) {fail(std::string(key)+" must be an array");continue;}
                for(const auto& element:signal[key]) {
                    if(!element.is_object() || !number(element,"resistance_ohm",true) || !locations.count(text(element,"at"))) {fail(std::string(key)+" requires known at and positive resistance_ohm");continue;}
                    if(element.contains("other_at") && !locations.count(text(element,"other_at"))) fail("termination other_at must name a known location");
                    if(element.contains("voltage_v") && !number(element,"voltage_v",false)) fail("bias/termination voltage_v must be finite nonnegative");
                    if(std::string(key)=="bias") {
                        std::string to=text(element,"to");
                        if(to!="ground" && to!="rail" && to!="differential") fail("bias to must be ground/rail/differential");
                        if(to!="ground" && !number(element,"voltage_v",false)) fail("rail/differential bias requires voltage_v");
                    }
                }
            }
        }
        if(declared!=signals.at(protocol)) fail("all protocol signals must be declared (no partial electrical/ideal fallback)");
    }
    if(sc.contains("tracks") && sc["tracks"].is_array()) for(const auto& track:sc["tracks"]) if(track.is_object() && track.contains("link_id") && !ids.count(text(track,"link_id"))) errors.push_back("unknown track link_id");
    if(sc.contains("tracks") && sc["tracks"].is_array()) for(const auto& track:sc["tracks"]) {
        if(!track.is_object() || !track.contains("link_id")) continue;
        for(const auto& link:sc["bus_links"]) if(link.is_object() && link.value("id",json())==track["link_id"]) {
            if(track.contains("bus") && track["bus"]!=link.value("bus",json())) errors.push_back("track bus conflicts with link_id");
            std::string kind=text(track,"kind"),protocol=text(link,"protocol");
            if((kind.rfind("i2c_",0)==0 && protocol!="i2c") || (kind.rfind("spi_",0)==0 && protocol!="spi") ||
               (kind=="uart_rx" && protocol!="uart") || (kind=="can_frames" && protocol!="can"))
                errors.push_back("track protocol conflicts with link_id");
        }
    }
    return errors;
}

json resolve_bus_links(const json& sc,const json& caps,const std::string& platform,std::vector<std::string>& errors) {
    json resolved=json::array();
    if(!sc.is_object() || !sc.contains("bus_links")) return resolved;
    auto invalid=validate_bus_links(sc);errors.insert(errors.end(),invalid.begin(),invalid.end());if(!invalid.empty()) return resolved;
    for(auto link:sc["bus_links"]) {
        if(caps.value("family",std::string())!="stm32f4") {errors.push_back("timed electrical bus models support STM32F4 only; selected target unsupported");continue;}
        for(auto& signal:link["electrical"]["signals"]) if(signal.contains("segments"))
            for(auto& segment:signal["segments"]) resolve_segment(segment);
        std::string bus=text(link,"bus"), protocol=text(link,"protocol");
        if(platform.find(bus+":")==std::string::npos) {errors.push_back("bus link selects absent controller '"+bus+"'");continue;}
        std::string prefix=protocol=="uart"?"usart":protocol;
        if(bus.rfind(prefix,0)!=0) {errors.push_back("bus link protocol/controller mismatch '"+bus+"'");continue;}
        if(!link.contains("limitations")) link["limitations"]=json::array();
        for(const char* limitation:{"Linear RC / lossless Branin; no nonlinear clamps, common-mode, asymmetry, EMC, crosstalk or dispersion", "rise_s/fall_s are 10-90 percent exponential source transition times", "Initial stored energy is zero; declared biases charge the medium", "Lumped series resistance is split equally between each endpoint location and shared wire node"})
            link["limitations"].push_back(limitation);
        link["limitations"].push_back("ECAD impedance assumes continuous reference plane(s); whole-net copper length is not an endpoint path. Explicit segment topology is authoritative");
        link["limitations"].push_back("Electrical integration uses backward Euler, timestep <= smallest line delay/8 and approximate local RC/16, floor 0.1ns; receiver schedules are nanosecond-quantized");
        if(protocol=="spi") link["limitations"].push_back("Legacy full-duplex/RXONLY master only; slave controller, I2S and one-wire BIDIMODE unsupported");
        resolved.push_back(std::move(link));
    }
    return resolved;
}

std::string generated_bus_source(const json& resolved) {
    std::string out;
    out.reserve(sizeof(kFirmwareBusRuntime)+sizeof(kFirmwareTimedI2C)+sizeof(kFirmwareTimedUART)+sizeof(kFirmwareTimedSPI)+sizeof(kFirmwareTimedCAN));
    out.append(kFirmwareBusRuntime).append(kFirmwareTimedI2C).append(kFirmwareTimedUART).append(kFirmwareTimedSPI).append(kFirmwareTimedCAN);
    // Each maintained file has using directives: keep those at compilation-unit start.
    std::istringstream in(out);std::string line,body,usings;std::set<std::string> seen;
    while(std::getline(in,line)) {if(line.rfind("using ",0)==0) {if(seen.insert(line).second) usings+=line+"\n";} else body+=line+"\n";}
    out=usings+body;
    out+="\nnamespace Antmicro.Renode.Peripherals.Chudware { public static class BusConfiguration { public static BusConfig For(string bus) { switch(bus) {\n";
    for(const auto& link:resolved) out+="case "+json(text(link,"bus")).dump()+": return new BusConfig("+literal(link)+");\n";
    return out+"default: return new BusConfig(null); } } } }\n";
}

json bus_reference_circuit(const json& signal) {
    json devices=json::array();int seq=0;
    auto device=[&](const std::string& kind,const json& nodes,const json& params) {
        json item={{"name",kind+std::to_string(seq++)},{"kind",kind},{"nodes",nodes},{"params",params}};
        if(params.contains("value")) {item["value"]=params["value"];item["params"].erase("value");}
        devices.push_back(std::move(item));
    };
    for(const auto& segment:signal.value("segments",json::array())) device("T",{segment.at("from"),"0",segment.at("to"),"0"},{{"z0",segment.at("z0_ohm")},{"td",segment.at("propagation_s")}});
    bool lumped=text(signal,"model")=="lumped_rc";double series=lumped?signal.at("series_r_ohm").get<double>():0;
    std::set<std::string> locations;
    for(const auto& ep:signal.at("endpoints")) {
        std::string at=series==0 && lumped?"wire":text(ep,"at");locations.insert(at);
        if(ep.at("input_capacitance_f").get<double>()>0) device("C",{at,"0"},{{"value",ep.at("input_capacitance_f")}});
        if(ep.contains("reference_waveform")) {
            std::string source="drive"+std::to_string(seq);device("V",{source,"0"},ep.at("reference_waveform"));
            device("R",{source,at},{{"value",ep.at("r_low_ohm")}});
        }
    }
    if(lumped) {
        if(series>0) for(const auto& at:locations) device("R",{at,"wire"},{{"value",series/2}});
        if(signal.at("wire_capacitance_f").get<double>()>0) device("C",{"wire","0"},{{"value",signal.at("wire_capacitance_f")}});
    }
    for(const char* key:{"bias","terminations"}) for(const auto& r:signal.value(key,json::array())) {
        std::string at=lumped&&series==0?"wire":text(r,"at"),other="0";
        if(r.contains("other_at")) other=lumped&&series==0?"wire":text(r,"other_at");
        else if(r.value("voltage_v",0.0)!=0) {other="rail"+std::to_string(seq);device("V",{other,"0"},{{"value",r.at("voltage_v")}});}
        device("R",{at,other},{{"value",r.at("resistance_ohm")}});
    }
    return {{"devices",devices},{"models",json::object()}};
}
json simulate_bus_reference(const json& signal,const json& study) {return sim::spice_simulate(bus_reference_circuit(signal),study);}
}  // namespace chudware::firmware
