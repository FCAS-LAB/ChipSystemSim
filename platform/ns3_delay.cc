// Minimal ns-3 delay oracle for LEGOSim matmul message transfers.
// It instantiates a point-to-point link and reports the simulated reception time.
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"

#include <cstdlib>
#include <iostream>

using namespace ns3;

static Time g_received_at = Seconds(0);

static void ReceiveTrace(Ptr<const Packet>) {
  g_received_at = Simulator::Now();
}

int main(int argc, char* argv[]) {
  uint32_t bytes = 1;
  uint32_t bandwidth_mbps = 1000;
  uint32_t propagation_us = 10;
  CommandLine command_line;
  command_line.AddValue("bytes", "Packet payload bytes", bytes);
  command_line.AddValue("bandwidth-mbps", "Point-to-point capacity", bandwidth_mbps);
  command_line.AddValue("propagation-us", "One-way propagation delay", propagation_us);
  command_line.Parse(argc, argv);

  NodeContainer nodes;
  nodes.Create(2);
  PointToPointHelper link;
  link.SetDeviceAttribute("DataRate", StringValue(std::to_string(bandwidth_mbps) + "Mbps"));
  link.SetChannelAttribute("Delay", TimeValue(MicroSeconds(propagation_us)));
  NetDeviceContainer devices = link.Install(nodes);

  devices.Get(1)->TraceConnectWithoutContext("MacRx", MakeCallback(&ReceiveTrace));
  Ptr<Packet> packet = Create<Packet>(bytes);
  devices.Get(0)->Send(packet, devices.Get(1)->GetAddress(), 0x0800);
  Simulator::Run();
  std::cout << g_received_at.GetNanoSeconds() << std::endl;
  Simulator::Destroy();
  return g_received_at.IsZero() ? 2 : 0;
}
