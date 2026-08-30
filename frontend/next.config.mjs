/** @type {import('next').NextConfig} */
const nextConfig = {
  // Docker 部署：standalone 产物自包含最小 server，运行镜像无需 node_modules
  output: "standalone",
};

export default nextConfig;
