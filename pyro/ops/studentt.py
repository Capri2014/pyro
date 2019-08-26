import math

import torch
from torch.distributions.utils import lazy_property
from torch.nn.functional import pad

from pyro.distributions.util import broadcast_shape


class Gamma:
    """
    Non-normalized Gamma distribution.
    """
    def __init__(self, log_normalizer, alpha, beta):
        self.log_normalizer = log_normalizer
        self.alpha = alpha
        self.beta = beta

    def log_density(self, s):
        """
        Non-normalized log probability of Gamma distribution.

        This is mainly used for testing.
        """
        return self.log_normalizer + (self.alpha - 1) * s.log() - self.beta * s

    def logsumexp(self):
        """
        Integrates out the latent variable.
        """
        return self.log_normalizer + torch.lgamma(self.alpha) - self.alpha * self.beta.log()


class GaussianGamma:
    """
    Non-normalized GaussianGamma distribution:

        GaussianGamma(x, s) ~ (alpha + 0.5 * dim - 1) * log(s)
                              - (beta + 0.5 * info_vec.T @ inv(precision) @ info_vec) * s
                              - 0.5 * s * x.T @ precision @ x + s * x.T @ info_vec,

    which will be reparameterized as

        GaussianGamma(x, s) =: alpha' * log(s) + s * (-0.5 * x.T @ precision @ x + x.T @ info_vec - beta').

    This represents an arbitrary semidefinite quadratic function, which can be
    interpreted as a rank-deficient scaled Gaussian distribution. The precision
    matrix may have zero eigenvalues, thus it may be impossible to work
    directly with the covariance matrix.

    :param torch.Tensor log_normalizer: a normalization constant, which is mainly used to keep
        track of normalization terms during contractions.
    :param torch.Tensor info_vec: information vector, which is a scaled version of the mean
        ``info_vec = precision @ mean``. We use this represention to make gaussian contraction
        fast and stable.
    :param torch.Tensor precision: precision matrix of this gaussian.
    :param torch.Tensor alpha: reparameterized shape parameter of the marginal Gamma distribution of
        `s`. The shape parameter Gamma.alpha is reparameterized by:

            alpha = Gamma.alpha + 0.5 * dim - 1

    :param torch.Tensor beta: reparameterized rate parameter of the marginal Gamma distribution of
        `s`. The rate parameter Gamma.beta is reparameterized by:

            beta = Gamma.beta + 0.5 * info_vec.T @ inv(precision) @ info_vec
    """
    def __init__(self, log_normalizer, info_vec, precision, alpha, beta):
        # NB: using info_vec instead of mean to deal with rank-deficient problem
        assert info_vec.dim() >= 1
        assert precision.dim() >= 2
        assert precision.shape[-2:] == info_vec.shape[-1:] * 2
        self.log_normalizer = log_normalizer
        self.info_vec = info_vec
        self.precision = precision
        self.alpha = alpha
        self.beta = beta

    def dim(self):
        return self.info_vec.size(-1)

    @lazy_property
    def batch_shape(self):
        return broadcast_shape(self.log_normalizer.shape,
                               self.info_vec.shape[:-1],
                               self.precision.shape[:-2],
                               self.alpha.shape,
                               self.beta.shape)

    def expand(self, batch_shape):
        n = self.dim()
        log_normalizer = self.log_normalizer.expand(batch_shape)
        info_vec = self.info_vec.expand(batch_shape + (n,))
        precision = self.precision.expand(batch_shape + (n, n))
        alpha = self.alpha.expand(batch_shape)
        beta = self.beta.expand(batch_shape)
        return GaussianGamma(log_normalizer, info_vec, precision, alpha, beta)

    def reshape(self, batch_shape):
        n = self.dim()
        log_normalizer = self.log_normalizer.reshape(batch_shape)
        info_vec = self.info_vec.reshape(batch_shape + (n,))
        precision = self.precision.reshape(batch_shape + (n, n))
        alpha = self.alpha.reshape(batch_shape)
        beta = self.beta.reshape(batch_shape)
        return GaussianGamma(log_normalizer, info_vec, precision, alpha, beta)

    def __getitem__(self, index):
        """
        Index into the batch_shape of a GaussianGamma.
        """
        assert isinstance(index, tuple)
        log_normalizer = self.log_normalizer[index]
        info_vec = self.info_vec[index + (slice(None),)]
        precision = self.precision[index + (slice(None), slice(None))]
        alpha = self.alpha[index]
        beta = self.beta[index]
        return GaussianGamma(log_normalizer, info_vec, precision, alpha, beta)

    @staticmethod
    def cat(parts, dim=0):
        """
        Concatenate a list of GaussianGammas along a given batch dimension.
        """
        if dim < 0:
            dim += len(parts[0].batch_shape)
        args = [torch.cat([getattr(g, attr) for g in parts], dim=dim)
                for attr in ["log_normalizer", "info_vec", "precision", "alpha", "beta"]]
        return GaussianGamma(*args)

    def event_pad(self, left=0, right=0):
        """
        Pad along event dimension.
        """
        lr = (left, right)
        info_vec = pad(self.info_vec, lr)
        precision = pad(self.precision, lr + lr)
        # no change for alpha, beta because we are working with reparameterized version
        return GaussianGamma(self.log_normalizer, info_vec, precision, self.alpha, self.beta)

    def event_permute(self, perm):
        """
        Permute along event dimension.
        """
        assert isinstance(perm, torch.Tensor)
        assert perm.shape == (self.dim(),)
        info_vec = self.info_vec[..., perm]
        precision = self.precision[..., perm][..., perm, :]
        return GaussianGamma(self.log_normalizer, info_vec, precision, self.alpha, self.beta)

    def __add__(self, other):
        """
        Adds two GaussianGammas in log-density space.
        """
        assert isinstance(other, GaussianGamma)
        assert self.dim() == other.dim()
        return GaussianGamma(self.log_normalizer + other.log_normalizer,
                             self.info_vec + other.info_vec,
                             self.precision + other.precision,
                             self.alpha + other.alpha,
                             self.beta + other.beta)

    def log_density(self, value, s):
        """
        Evaluate the log density of this GaussianGamma at a point value::

            alpha * log(s) + s * (-0.5 * value.T @ precision @ value + value.T @ info_vec - beta) + log_normalizer

        This is mainly used for testing.
        """
        if value.size(-1) == 0:
            batch_shape = broadcast_shape(value.shape[:-1], self.batch_shape)
            return self.alpha * s.log() - self.beta * s + self.log_normalizer.expand(batch_shape)
        result = (-0.5) * self.precision.matmul(value.unsqueeze(-1)).squeeze(-1)
        result = result + self.info_vec
        result = (value * result).sum(-1)
        return self.alpha * s.log() + (result - self.beta) * s + self.log_normalizer

    def condition(self, value):
        """
        Condition the Gaussian component on a trailing subset of its state.
        This should satisfy::

            g.condition(y).dim() == g.dim() - y.size(-1)

        Note that since this is a non-normalized Gaussian, we include the
        density of ``y`` in the result. Thus :meth:`condition` is similar to a
        ``functools.partial`` binding of arguments::

            left = x[..., :n]
            right = x[..., n:]
            g.log_density(x, s) == g.condition(right).log_density(left, s)
        """
        assert isinstance(value, torch.Tensor)
        assert value.size(-1) <= self.info_vec.size(-1)

        n = self.dim() - value.size(-1)
        info_a = self.info_vec[..., :n]
        info_b = self.info_vec[..., n:]
        P_aa = self.precision[..., :n, :n]
        P_ab = self.precision[..., :n, n:]
        P_bb = self.precision[..., n:, n:]
        b = value

        info_vec = info_a - P_ab.matmul(b.unsqueeze(-1)).squeeze(-1)
        precision = P_aa

        log_normalizer = self.log_normalizer
        alpha = self.alpha
        beta = self.beta - 0.5 * P_bb.matmul(b.unsqueeze(-1)).squeeze(-1).mul(b).sum(-1) + b.mul(info_b).sum(-1)
        return GaussianGamma(log_normalizer, info_vec, precision, alpha, beta)

    # TODO: port marginalize
    def marginalize(self, left=0, right=0):
        """
        Marginalizing out variables on either side of the event dimension::

            g.marginalize(left=n).event_logsumexp() = g.event_logsumexp()
            g.marginalize(right=n).event_logsumexp() = g.event_logsumexp()

        and for data ``x``:

            g.condition(x).event_logsumexp().log_density(s)
              = g.marginalize(left=g.dim() - x.size(-1)).log_density(x, s)
        """
        # NB: the easiest way to think about this process is to consider GaussianGamma as a Gaussian
        # with precision and info_vec scaled by `s`.
        if left == 0 and right == 0:
            return self
        if left > 0 and right > 0:
            raise NotImplementedError
        n = self.dim()
        n_b = left + right
        a = slice(left, n - right)  # preserved
        b = slice(None, left) if left else slice(n - right, None)

        P_aa = self.precision[..., a, a]
        P_ba = self.precision[..., b, a]
        P_bb = self.precision[..., b, b]
        P_b = P_bb.cholesky()
        P_a = P_ba.triangular_solve(P_b, upper=False).solution
        P_at = P_a.transpose(-1, -2)
        precision = P_aa - P_at.matmul(P_a)

        info_a = self.info_vec[..., a]
        info_b = self.info_vec[..., b]
        b_tmp = info_b.unsqueeze(-1).triangular_solve(P_b, upper=False).solution
        info_vec = info_a - P_at.matmul(b_tmp).squeeze(-1)

        log_normalizer = (self.log_normalizer +
                          0.5 * n_b * math.log(2 * math.pi) -
                          P_b.diagonal(dim1=-2, dim2=-1).log().sum(-1) +
                          0.5 * b_tmp.squeeze(-1).pow(2).sum(-1))
        return GaussianGamma(log_normalizer, info_vec, precision)

    def event_logsumexp(self):
        """
        Integrates out all latent state (i.e. operating on event dimensions) of Gaussian component.
        """
        n = self.dim()
        chol_P = self.precision.cholesky()
        chol_P_u = self.info_vec.unsqueeze(-1).triangular_solve(chol_P, upper=False).solution.squeeze(-1)
        u_P_u = chol_P_u.pow(2).sum(-1)
        # considering GaussianGamma as a Gaussian with precision = s * precision, info_vec = s * info_vec,
        # marginalize x variable, we get
        #   logsumexp(s) = alpha' * log(s) - s * beta' + 0.5 n * log(2 pi) + 0.5 s * uPu - 0.5 * |P| - 0.5 n * s
        # use the original parameterization of Gamma, we get
        #   logsumexp(s) = (alpha - 1) * log(s) - s * beta + 0.5 n * log(2 pi) - 0.5 * |P|
        # Note that `(alpha - 1) * log(s) - s * beta` is unnormalized log_prob of Gamma(alpha, beta)
        alpha = self.alpha - 0.5 * n + 1
        beta = self.beta - 0.5 * u_P_u
        log_normalizer_tmp = 0.5 * n * math.log(2 * math.pi) - chol_P.diagonal(dim1=-2, dim2=-1).log().sum(-1)
        return Gamma(self.log_normalizer + log_normalizer_tmp, alpha, beta)
