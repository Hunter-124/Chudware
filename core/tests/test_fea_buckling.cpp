#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include "sim/fea_buckling.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <map>
#include <vector>

namespace {
struct Column {
    std::vector<double> points, forces;
    std::vector<int> tets, fixed;
};
Column column(bool quadratic, double load) {
    Column c;
    const int nx=3, ny=3, nz=50;
    auto node = [&](int i,int j,int k) { return (k*(ny+1)+j)*(nx+1)+i; };
    for (int k=0;k<=nz;++k)
        for (int j=0;j<=ny;++j)
            for (int i=0;i<=nx;++i) {
                c.points.push_back(10.0*i/nx);
                c.points.push_back(10.0*j/ny);
                c.points.push_back(200.0*k/nz);
            }
    constexpr int split[6][4]={{0,1,2,6},{0,2,3,6},{0,3,7,6},
                              {0,7,4,6},{0,4,5,6},{0,5,1,6}};
    constexpr int edges[6][2]={{0,1},{1,2},{2,0},{3,0},{3,2},{3,1}};
    std::map<std::pair<int,int>,int> midpoints;
    auto midpoint = [&](int a,int b) {
        auto key=std::minmax(a,b);
        auto found=midpoints.find(key);
        if (found!=midpoints.end()) return found->second;
        const int id=static_cast<int>(c.points.size()/3);
        for(int d=0;d<3;++d) c.points.push_back((c.points[3*a+d]+c.points[3*b+d])*0.5);
        midpoints.emplace(key,id);
        return id;
    };
    for(int k=0;k<nz;++k) for(int j=0;j<ny;++j) for(int i=0;i<nx;++i) {
        const int corners[8]={node(i,j,k),node(i+1,j,k),node(i+1,j+1,k),node(i,j+1,k),
                              node(i,j,k+1),node(i+1,j,k+1),node(i+1,j+1,k+1),node(i,j+1,k+1)};
        for(const auto& tet:split) {
            for(int v:tet) c.tets.push_back(corners[v]);
            if(quadratic) for(const auto& edge:edges)
                c.tets.push_back(midpoint(corners[tet[edge[0]]],corners[tet[edge[1]]]));
        }
    }
    c.forces.assign(c.points.size(),0.0);
    for(int n=0;n<static_cast<int>(c.points.size()/3);++n)
        if(c.points[3*n+2]==0.0) c.fixed.push_back(n);
    // Uniform end traction integrated with the actual triangular face shape functions.
    for(int j=0;j<ny;++j) for(int i=0;i<nx;++i) {
        const int a=node(i,j,nz), b=node(i+1,j,nz), d=node(i,j+1,nz), e=node(i+1,j+1,nz);
        const int triangles[2][3]={{a,b,e},{a,e,d}};
        for(const auto& tri:triangles) for(int p=0;p<3;++p) {
            const int n=quadratic ? midpoint(tri[p],tri[(p+1)%3]) : tri[p];
            c.forces[3*n+2]+=load/(2.0*nx*ny*3.0);
        }
    }
    return c;
}
}

TEST_CASE("tet10 h4 fixed-free Euler column and reference-load scaling") {
    auto c=column(true,-1000.0);
    auto solve=[&] { return chudware::sim::fea_buckling_solve(
        c.points,c.tets,200000.0,0.3,c.fixed,c.forces,4,10); };
    const auto r=solve();
    INFO(r.error);
    REQUIRE(r.ok);
    REQUIRE(r.load_factors.size()==4);
    const double expected=std::acos(-1.0)*std::acos(-1.0)*200000.0*(10000.0/12.0)/(4.0*200.0*200.0);
    const double critical=r.load_factors[0]*1000.0;
    std::printf("Euler fixed-free: critical %.8f N; reference %.8f N; error %.5f%%\n",
                critical,expected,100.0*std::abs(critical/expected-1.0));
    CHECK(std::abs(critical/expected-1.0)<0.05);
    CHECK(r.load_factors[1]/r.load_factors[0]<1.02);
    for(int n:c.fixed) for(int m=0;m<r.n_modes;++m) for(int d=0;d<3;++d)
        CHECK(r.mode_shapes[(static_cast<size_t>(m)*r.n_nodes+n)*3+d]==0.0);
    for(double& force:c.forces) force*=2.0;
    const auto doubled=solve();
    REQUIRE(doubled.ok);
    CHECK(doubled.load_factors[0]*2.0==doctest::Approx(r.load_factors[0]).epsilon(1e-5));
    const auto capped=chudware::sim::fea_buckling_solve(c.points,c.tets,200000.0,0.3,
        c.fixed,c.forces,4,10,1.0);
    CHECK_FALSE(capped.ok);
    CHECK(capped.load_factors.empty());
}

TEST_CASE("tensile reference load has no positive buckling modes") {
    for(bool quadratic:{false,true}) {
        auto c=column(quadratic,1000.0);
        const auto r=chudware::sim::fea_buckling_solve(c.points,c.tets,200000.0,0.3,
            c.fixed,c.forces,4,quadratic?10:4);
        INFO(r.error);
        CHECK_FALSE(r.ok);
        CHECK(r.load_factors.empty());
    }
}
